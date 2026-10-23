// Package broker plánuje úlohy na SPARKu: okno na vyžádání, jinak podle priority.
//
// Jedna smyčka (Tick) rozhoduje o všem: kdo běží, kdy přepnout profil, koho
// odsunout. Provisioning (přepnutí profilu, start doplňku) běží v gorutině a
// během něj se nic dalšího nerozhoduje — dvě přepnutí naráz byla přesně ten
// typ chyby, kvůli kterému broker vzniká (23. 10. 08:03).
package broker

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/catalog"
)

type Notifier interface{ Send(text string) }

type Options struct {
	StatePath        string
	Control          bool // false = advise: nic nespouští, jen eviduje a říká, kdy jde začít
	Async            bool // provisioning v gorutině (testy false)
	Now              func() time.Time
	HeartbeatTimeout time.Duration
	DrainGrace       time.Duration
	IdleCloseGrace   time.Duration // po varování o nečinnosti
	DetectEvery      time.Duration
}

type Broker struct {
	mu    sync.Mutex
	cat   *catalog.Catalog
	ex    Executor
	nt    Notifier
	opt   Options
	st    *persisted
	procs map[string]Proc

	busy       string // probíhající provisioning (popis), "" = nic
	lastDetect time.Time
	probe      Probe
	lastComfy  int64
	hotSince   *time.Time
	hotWarned  bool
	kick       chan struct{}
}

var ErrNotFound = errors.New("úloha neexistuje")

type HTTPError struct {
	Code int
	Msg  string
}

func (e *HTTPError) Error() string { return e.Msg }

func bad(f string, a ...any) error { return &HTTPError{400, fmt.Sprintf(f, a...)} }
func conflict(f string, a ...any) error {
	return &HTTPError{409, fmt.Sprintf(f, a...)}
}

func New(cat *catalog.Catalog, ex Executor, nt Notifier, opt Options) (*Broker, error) {
	if opt.Now == nil {
		opt.Now = time.Now
	}
	if opt.HeartbeatTimeout == 0 {
		opt.HeartbeatTimeout = 10 * time.Minute
	}
	if opt.DrainGrace == 0 {
		opt.DrainGrace = 5 * time.Minute
	}
	if opt.IdleCloseGrace == 0 {
		opt.IdleCloseGrace = 15 * time.Minute
	}
	if opt.DetectEvery == 0 {
		opt.DetectEvery = time.Minute
	}
	st, err := load(opt.StatePath)
	if err != nil {
		return nil, err
	}
	b := &Broker{cat: cat, ex: ex, nt: nt, opt: opt, st: st, procs: map[string]Proc{}, lastComfy: -1,
		kick: make(chan struct{}, 1)}
	now := opt.Now()
	// Po restartu brokera (nebo SPARKu) běžící run úlohy nemají proces —
	// resumovatelné jdou zpět do fronty, ostatní jsou chyba k ručnímu řešení.
	for _, j := range st.Jobs {
		if !j.Active() || j.Stage() == nil || j.Stage().Mode != ModeRun {
			continue
		}
		if j.Resume {
			j.State = Queued
			j.event(now, "broker restartoval, úloha jde znovu do fronty")
		} else {
			b.fail(j, now, "broker restartoval uprostřed běhu a úloha neumí resume")
		}
	}
	return b, nil
}

func (b *Broker) Kick() {
	select {
	case b.kick <- struct{}{}:
	default:
	}
}

// Run točí plánovač, dokud ctx neskončí.
func (b *Broker) Run(ctx context.Context, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		b.refreshProbe(ctx)
		b.Tick()
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		case <-b.kick:
		}
	}
}

func (b *Broker) refreshProbe(ctx context.Context) {
	p := b.ex.Probe(ctx)
	b.mu.Lock()
	b.probe = p
	b.mu.Unlock()
}

// SetProbe pro testy.
func (b *Broker) SetProbe(p Probe) {
	b.mu.Lock()
	b.probe = p
	b.mu.Unlock()
}

// ---------------------------------------------------------------- požadavky

type SubmitReq struct {
	Project        string     `json:"project"`
	Session        string     `json:"session"`
	Title          string     `json:"title"`
	Priority       int        `json:"priority"`
	Deadline       *time.Time `json:"deadline"`
	Preemptible    *bool      `json:"preemptible"`
	Resume         bool       `json:"resume"`
	Heartbeat      *bool      `json:"heartbeat"`
	IdleCloseMin   int        `json:"idle_close_min"`
	OnBehalfOfUser bool       `json:"on_behalf_of_user"`
	UserQuote      string     `json:"user_quote"`
	Notify         *bool      `json:"notify"`
	Stages         []Stage    `json:"stages"`
}

func (b *Broker) Submit(r SubmitReq) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	if r.Project == "" {
		return nil, bad("chybí project")
	}
	if len(r.Stages) == 0 {
		return nil, bad("chybí stages (aspoň jedna: services + mode)")
	}
	if r.OnBehalfOfUser && strings.TrimSpace(r.UserQuote) == "" {
		return nil, bad("on_behalf_of_user chce user_quote — doslovná slova uživatele, kterými o okno požádal")
	}
	for i := range r.Stages {
		s := &r.Stages[i]
		if s.Name == "" {
			s.Name = fmt.Sprintf("stage%d", i+1)
		}
		if len(s.Services) == 0 {
			return nil, bad("stage %s: chybí services", s.Name)
		}
		for _, svc := range s.Services {
			if !b.cat.Known(svc) {
				return nil, bad("stage %s: neznámá služba %q (GET /v1/catalog)", s.Name, svc)
			}
		}
		if _, ok := b.cat.Target("", s.Services); !ok {
			return nil, bad("stage %s: služby %v se nevejdou do žádného profilu spolu — rozděl je do víc stages", s.Name, s.Services)
		}
		switch s.Mode {
		case ModeRun:
			if s.Command == "" {
				return nil, bad("stage %s: run chce command", s.Name)
			}
		case ModeLease, "":
			s.Mode = ModeLease
			if s.Command != "" {
				return nil, bad("stage %s: lease nemá command (příkaz spouští session sama)", s.Name)
			}
		default:
			return nil, bad("stage %s: mode je run nebo lease", s.Name)
		}
	}
	j := &Job{
		Project: r.Project, Session: r.Session, Title: r.Title, Deadline: r.Deadline,
		Resume: r.Resume, OnBehalfOfUser: r.OnBehalfOfUser, UserQuote: r.UserQuote,
		Stages: r.Stages, State: Queued, Created: now, Notify: true, RequestedPriority: r.Priority,
		IdleCloseMin: r.IdleCloseMin,
	}
	if j.Title == "" {
		j.Title = r.Stages[0].Name
	}
	if r.Notify != nil {
		j.Notify = *r.Notify
	}
	j.Preemptible = !r.OnBehalfOfUser
	if r.Preemptible != nil {
		j.Preemptible = *r.Preemptible
	}
	for _, s := range j.Stages {
		if s.Mode == ModeRun && j.Preemptible && !j.Resume {
			return nil, bad("run stage %s: preemptible úloha musí umět resume (jinak preemptible:false)", s.Name)
		}
	}
	j.Heartbeat = !r.OnBehalfOfUser
	if r.Heartbeat != nil {
		j.Heartbeat = *r.Heartbeat
	}
	if r.OnBehalfOfUser && j.IdleCloseMin == 0 && usesComfy(j) {
		j.IdleCloseMin = 30
	}
	b.setPriority(j, r.Priority, now)
	b.st.Seq++
	j.ID = fmt.Sprintf("%s-%d", now.Format("0102"), b.st.Seq)
	j.event(now, "zařazena (priorita %d)", j.Priority)
	b.st.Jobs = append(b.st.Jobs, j)
	if r.OnBehalfOfUser {
		b.note("🪟 <b>%s</b> · okno <b>%s</b> jménem uživatele (%s, priorita %d)\n„%s“\nzrušit: řekni Claudovi „zruš úlohu %s“",
			esc(j.Project), esc(stagesLabel(j)), esc(orDash(j.Session)), j.Priority, esc(j.UserQuote), j.ID)
	}
	b.saveLocked()
	b.Kick()
	return clone(j), nil
}

// setPriority uplatní strop projektu; vyšší přání čeká na souhlas uživatele.
func (b *Broker) setPriority(j *Job, want int, now time.Time) {
	limit := b.cat.ProjectMax(j.Project)
	if j.OnBehalfOfUser {
		limit = 100
		if want == 0 {
			want = 100
		}
	}
	if want < 0 {
		want = 0
	}
	if want > 100 {
		want = 100
	}
	j.PendingPriority = 0
	if want > limit {
		j.Priority = limit
		j.PendingPriority = want
		b.note("❓ <b>%s</b> · %s chce prioritu %d, strop projektu je %d.\nSchválit: řekni Claudovi „schval prioritu úlohy %s“. Do té doby jede s %d.",
			esc(j.Project), esc(j.Title), want, limit, j.ID, limit)
		j.event(now, "chce prioritu %d nad strop %d — čeká na uživatele", want, limit)
		return
	}
	j.Priority = want
}

type PatchReq struct {
	Priority *int       `json:"priority"`
	Deadline *time.Time `json:"deadline"`
	Title    *string    `json:"title"`
	Cancel   bool       `json:"cancel"`
}

func (b *Broker) Patch(id string, r PatchReq) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	j := b.find(id)
	if j == nil {
		return nil, ErrNotFound
	}
	if j.Final() {
		return nil, conflict("úloha už skončila (%s)", j.State)
	}
	if r.Cancel {
		b.cancel(j, now, "zrušeno klientem")
		b.saveLocked()
		b.Kick()
		return clone(j), nil
	}
	if r.Priority != nil {
		b.setPriority(j, *r.Priority, now)
		j.event(now, "priorita → %d", j.Priority)
	}
	if r.Deadline != nil {
		j.Deadline = r.Deadline
	}
	if r.Title != nil {
		j.Title = *r.Title
	}
	b.saveLocked()
	b.Kick()
	return clone(j), nil
}

func (b *Broker) Approve(id string) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	j := b.find(id)
	if j == nil {
		return nil, ErrNotFound
	}
	if j.PendingPriority == 0 {
		return nil, conflict("úloha nečeká na schválení priority")
	}
	j.Priority, j.PendingPriority = j.PendingPriority, 0
	j.event(now, "uživatel schválil prioritu %d", j.Priority)
	b.note("✔️ priorita %d pro <b>%s</b> · %s schválena", j.Priority, esc(j.Project), esc(j.Title))
	b.saveLocked()
	b.Kick()
	return clone(j), nil
}

type DoneReq struct {
	Metrics map[string]Count `json:"metrics"`
	Error   string           `json:"error"` // neprázdné = stage selhala
}

func (b *Broker) Heartbeat(id string, m map[string]Count) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	j := b.find(id)
	if j == nil {
		return nil, ErrNotFound
	}
	if j.Final() {
		return clone(j), conflict("úloha už skončila (%s)", j.State)
	}
	j.LastHeartbeat = &now
	j.addMetrics(m)
	b.saveLocked()
	return clone(j), nil
}

func (b *Broker) Done(id string, r DoneReq) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	j := b.find(id)
	if j == nil {
		return nil, ErrNotFound
	}
	if !j.Active() || j.Stage() == nil || j.Stage().Mode != ModeLease {
		return nil, conflict("done jen pro běžící lease (stav %s)", j.State)
	}
	j.addMetrics(r.Metrics)
	if r.Error != "" {
		b.fail(j, now, r.Error)
	} else {
		b.finishStage(j, now, "session ohlásila konec")
	}
	b.saveLocked()
	b.Kick()
	return clone(j), nil
}

func (b *Broker) Extend(id string, minutes int) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	j := b.find(id)
	if j == nil {
		return nil, ErrNotFound
	}
	if j.State != Running || j.Stage().Mode != ModeLease {
		return nil, conflict("prodloužit jde jen běžící lease (stav %s)", j.State)
	}
	if minutes <= 0 {
		minutes = 30
	}
	for _, q := range b.queue() {
		if q.Priority > j.Priority && !b.fitsCurrent(q) {
			return nil, conflict("nejde: čeká %s (%s, priorita %d) a potřebuje jiný profil; lease končí %s",
				q.ID, q.Project, q.Priority, j.LeaseUntil.Format("15:04"))
		}
	}
	base := now
	if j.LeaseUntil != nil && j.LeaseUntil.After(now) {
		base = *j.LeaseUntil
	}
	t := base.Add(time.Duration(minutes) * time.Minute)
	j.LeaseUntil = &t
	j.IdleWarned = false
	j.event(now, "prodlouženo do %s", t.Format("15:04"))
	b.saveLocked()
	return clone(j), nil
}

func (b *Broker) SetPaused(p bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.st.Paused = p
	b.saveLocked()
	b.Kick()
}

func (b *Broker) Get(id string) (*Job, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	j := b.find(id)
	if j == nil {
		return nil, ErrNotFound
	}
	return clone(j), nil
}

func (b *Broker) List(project string, all bool) []*Job {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []*Job
	for _, j := range b.st.Jobs {
		if project != "" && j.Project != project {
			continue
		}
		if !all && j.Final() {
			continue
		}
		out = append(out, clone(j))
	}
	return out
}

type Status struct {
	Mode         string    `json:"mode"`
	Profile      string    `json:"profile"`
	ProfileSince time.Time `json:"profile_since"`
	Addons       []string  `json:"addons"`
	Busy         string    `json:"busy,omitempty"`
	Paused       bool      `json:"paused"`
	Probe        Probe     `json:"probe"`
	Active       []*Job    `json:"active"`
	Queue        []*Job    `json:"queue"`
	Hint         string    `json:"hint"`
}

func (b *Broker) Status() Status {
	b.mu.Lock()
	defer b.mu.Unlock()
	s := Status{Mode: "advise", Profile: b.st.Profile, ProfileSince: b.st.ProfileSince, Busy: b.busy,
		Paused: b.st.Paused, Probe: b.probe}
	if b.opt.Control {
		s.Mode = "control"
	}
	for a, up := range b.st.Addons {
		if up {
			s.Addons = append(s.Addons, a)
		}
	}
	sort.Strings(s.Addons)
	for _, j := range b.st.Jobs {
		if j.Active() {
			s.Active = append(s.Active, slim(j))
		}
	}
	for _, j := range b.queue() {
		s.Queue = append(s.Queue, slim(j))
	}
	s.Hint = "Úlohu zařaď POST /v1/jobs, pak čekej na state=running (GET /v1/jobs/{id}, nejvýš 1× za minutu)."
	if !b.opt.Control {
		s.Hint += " Režim advise: broker nic nespouští — running znamená, že potřebné služby právě běží."
	}
	return s
}

// ---------------------------------------------------------------- plánovač

// Tick udělá jedno kolo rozhodování.
func (b *Broker) Tick() {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	b.recurring(now)
	b.digest(now)
	b.detect(now)
	b.watchLeases(now)
	defer b.saveLocked()
	if b.busy != "" {
		return
	}
	b.releaseAddons(now)
	if b.busy != "" {
		return
	}
	b.schedule(now)
}

func (b *Broker) queue() []*Job {
	var q []*Job
	for _, j := range b.st.Jobs {
		if j.State == Queued {
			q = append(q, j)
		}
	}
	sort.SliceStable(q, func(a, c int) bool {
		x, y := q[a], q[c]
		if x.Priority != y.Priority {
			return x.Priority > y.Priority
		}
		if fx, fy := b.fitsCurrent(x), b.fitsCurrent(y); fx != fy {
			return fx // stejná priorita: bez přepnutí vyhrává (přepnutí stojí 2–10 min)
		}
		if (x.Deadline == nil) != (y.Deadline == nil) {
			return x.Deadline != nil
		}
		if x.Deadline != nil && !x.Deadline.Equal(*y.Deadline) {
			return x.Deadline.Before(*y.Deadline)
		}
		return x.Created.Before(y.Created)
	})
	for i, j := range q {
		j.Position = i + 1
	}
	return q
}

func (b *Broker) fitsCurrent(j *Job) bool {
	if b.st.Profile == "" || j.Stage() == nil {
		return false
	}
	_, ok := b.cat.Fits(b.st.Profile, j.Stage().Services)
	return ok
}

func (b *Broker) schedule(now time.Time) {
	if b.st.Paused {
		for _, j := range b.queue() {
			j.Waiting = "broker je pozastavený (uživatel nebo chyba přepnutí)"
		}
		return
	}
	if b.probe.GPUTempC >= b.cat.TempPauseC {
		if b.hotSince == nil {
			b.hotSince = &now
		}
		if !b.hotWarned && now.Sub(*b.hotSince) > 10*time.Minute {
			b.hotWarned = true
			b.alert("GPU má %d °C přes 10 min, nové úlohy nestartují. Zkontroluj větrák a teplotu.", b.probe.GPUTempC)
		}
		for _, j := range b.queue() {
			j.Waiting = fmt.Sprintf("teplotní pauza (GPU %d °C)", b.probe.GPUTempC)
		}
		return
	}
	b.hotSince, b.hotWarned = nil, false

	blockedHigher := false // vyšší úloha čeká na přepnutí — dál smí jen to, co jde kdykoli odsunout
	for _, j := range b.queue() {
		st := j.Stage()
		target, _ := b.cat.Target(b.st.Profile, st.Services)
		if target == b.st.Profile && b.st.Profile != "" {
			if blockedHigher && !j.Preemptible {
				j.Waiting = "čeká, až se uvolní SPARK pro úlohu s vyšší prioritou"
				continue
			}
			addons, _ := b.cat.Fits(target, st.Services)
			if why := b.addonsBusy(addons); why != "" {
				j.Waiting = why
				continue
			}
			var missing []string
			for _, a := range addons {
				if !b.st.Addons[a] {
					missing = append(missing, a)
				}
			}
			if len(missing) > 0 {
				if !b.opt.Control {
					j.Waiting = fmt.Sprintf("advise: doplněk %v neběží", missing)
					continue
				}
				b.startAddons(j, missing, now)
				return // provisioning běží; další kolo po něm
			}
			if st.Mode == ModeRun && !b.opt.Control {
				j.Waiting = "advise: run úlohy spouští broker až v režimu control"
				continue
			}
			b.start(j, now)
			continue
		}
		if blockedHigher {
			j.Waiting = fmt.Sprintf("potřebuje profil %s; před ní čeká úloha s vyšší prioritou", target)
			continue
		}
		blockedHigher = true
		if !b.opt.Control {
			j.Waiting = fmt.Sprintf("advise: potřebuje profil %s, teď běží %s", target, orDash(b.st.Profile))
			continue
		}
		// Přepnout jde, jen když všechno, co běží, má nižší prioritu a dá se odsunout.
		var active []*Job
		blocker := ""
		for _, a := range b.st.Jobs {
			if !a.Active() {
				continue
			}
			active = append(active, a)
			if !a.Preemptible || a.Priority >= j.Priority {
				blocker = fmt.Sprintf("%s (%s, priorita %d", a.ID, a.Project, a.Priority)
				if a.LeaseUntil != nil {
					blocker += ", do " + a.LeaseUntil.Format("15:04")
				}
				blocker += ")"
			}
		}
		if blocker != "" {
			j.Waiting = fmt.Sprintf("potřebuje profil %s, běží %s", target, blocker)
			continue
		}
		if len(active) > 0 {
			for _, a := range active {
				b.drain(a, now, fmt.Sprintf("SPARK potřebuje %s (%s, priorita %d)", j.ID, j.Project, j.Priority))
			}
			j.Waiting = fmt.Sprintf("odsouvám %d úloh(y), pak přepnu na %s", len(active), target)
			continue
		}
		j.Waiting = "přepínám na " + target
		b.switchProfile(target, j, now)
		return
	}
}

func (b *Broker) addonsBusy(addons []string) string {
	for _, a := range addons {
		max := b.cat.Addons[a].MaxJobs
		if max <= 0 {
			continue
		}
		n := 0
		for _, j := range b.st.Jobs {
			if j.Active() && containsStr(j.Stage().Services, a) {
				n++
			}
		}
		if n >= max {
			return fmt.Sprintf("%s je obsazený (%d/%d úloh)", a, n, max)
		}
	}
	return ""
}

func (b *Broker) start(j *Job, now time.Time) {
	st := j.Stage()
	j.State = Running
	j.Waiting = ""
	j.Position = 0
	j.Profile = b.st.Profile
	j.StageStarted = &now
	if j.Started == nil {
		j.Started = &now
	}
	j.LastHeartbeat, j.LastActivity, j.IdleWarned, j.DrainAt = &now, &now, false, nil
	if st.Mode == ModeLease {
		mins := st.LeaseMin
		if mins == 0 {
			mins = st.EstimateMin * 3 / 2
		}
		if mins < 15 {
			mins = 60
		}
		t := now.Add(time.Duration(mins) * time.Minute)
		if j.Recurring == "" || j.LeaseUntil == nil {
			j.LeaseUntil = &t
		}
		j.event(now, "%s: prostředí připravené (%s), lease do %s", st.Name, b.st.Profile, j.LeaseUntil.Format("15:04"))
		return
	}
	j.LeaseUntil = nil
	logDir := expandHome(b.cat.LogDir)
	if logDir == "" {
		logDir = os.TempDir()
	}
	_ = os.MkdirAll(logDir, 0o755)
	j.Log = filepath.Join(logDir, fmt.Sprintf("%s-%s.log", j.ID, st.Name))
	j.Attempts++
	p, err := b.ex.Start(st.Command, st.Dir, j.Log, []string{"SPARK_BROKER_JOB=" + j.ID})
	if err != nil {
		b.fail(j, now, "start selhal: "+err.Error())
		return
	}
	b.procs[j.ID] = p
	j.event(now, "%s: běží (%s), log %s", st.Name, b.st.Profile, j.Log)
	go func(id string, p Proc) {
		code, m := p.Wait()
		b.onExit(id, code, m)
	}(j.ID, p)
}

func (b *Broker) onExit(id string, code int, m map[string]Count) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.opt.Now()
	delete(b.procs, id)
	j := b.find(id)
	if j == nil || j.Final() {
		return
	}
	j.addMetrics(m)
	switch {
	case j.State == Draining:
		if code == 0 {
			b.finishStage(j, now, "doběhla během odsouvání")
		} else {
			j.State, j.DrainAt = Queued, nil
			j.event(now, "odsunuta (exit %d), pokračuje později", code)
		}
	case code == 0:
		b.finishStage(j, now, "exit 0")
	default:
		b.fail(j, now, fmt.Sprintf("exit %d\n%s", code, tail(j.Log, 8)))
	}
	b.saveLocked()
	b.Kick()
}

// drain požádá běžící úlohu o uvolnění SPARKu.
func (b *Broker) drain(j *Job, now time.Time, why string) {
	if j.State == Draining {
		return
	}
	t := now.Add(b.opt.DrainGrace)
	j.State, j.DrainAt = Draining, &t
	j.event(now, "odsouvám: %s", why)
	if p := b.procs[j.ID]; p != nil {
		p.Terminate()
	}
}

func (b *Broker) watchLeases(now time.Time) {
	comfyChanged := false
	if b.probe.ComfyActive >= 0 {
		comfyChanged = b.lastComfy >= 0 && b.probe.ComfyActive != b.lastComfy
		b.lastComfy = b.probe.ComfyActive
	}
	for _, j := range b.st.Jobs {
		if !j.Active() {
			continue
		}
		st := j.Stage()
		if j.State == Draining {
			if j.DrainAt != nil && now.After(*j.DrainAt) {
				if st.Mode == ModeLease {
					j.State, j.DrainAt = Queued, nil
					j.event(now, "lease odsunut, vrací se do fronty")
				} else if p := b.procs[j.ID]; p != nil && now.After(j.DrainAt.Add(b.opt.DrainGrace)) {
					p.Kill()
				}
			}
			continue
		}
		if st.Mode != ModeLease {
			continue
		}
		if j.Heartbeat && j.LastHeartbeat != nil && now.Sub(*j.LastHeartbeat) > b.opt.HeartbeatTimeout {
			j.State = Orphaned
			j.Finished = &now
			j.Error = fmt.Sprintf("žádný heartbeat %v — session nejspíš skončila (usage limit?)", b.opt.HeartbeatTimeout)
			j.event(now, "%s", j.Error)
			b.alert("⚠️ <b>%s</b> · %s: %s. Prostředí uvolněno; úloha patří projektu, nová session ji najde v GET /v1/jobs?project=%s&all=1.",
				esc(j.Project), esc(j.Title), esc(j.Error), esc(j.Project))
			continue
		}
		if j.IdleCloseMin > 0 && usesComfy(j) {
			if comfyChanged {
				j.LastActivity, j.IdleWarned = &now, false
			}
			idle := now.Sub(*j.LastActivity)
			limit := time.Duration(j.IdleCloseMin) * time.Minute
			if idle > limit+b.opt.IdleCloseGrace {
				b.finishStage(j, now, fmt.Sprintf("zavřeno po %d min nečinnosti", int(idle.Minutes())))
				continue
			}
			if idle > limit && !j.IdleWarned {
				j.IdleWarned = true
				b.note("💤 <b>%s</b> · okno %s je %d min nečinné, zavřu ho za %d min. Nechat: řekni Claudovi „prodluž okno %s“.",
					esc(j.Project), esc(stagesLabel(j)), int(idle.Minutes()), int(b.opt.IdleCloseGrace.Minutes()), j.ID)
			}
		}
		if j.LeaseUntil != nil && now.After(*j.LeaseUntil) {
			b.finishStage(j, now, "lease vypršel")
		}
	}
}

func (b *Broker) finishStage(j *Job, now time.Time, why string) {
	st := j.Stage()
	if st != nil {
		j.event(now, "%s hotovo: %s", st.Name, why)
	}
	j.StageIdx++
	j.LeaseUntil, j.DrainAt = nil, nil
	if j.StageIdx < len(j.Stages) {
		j.State = Queued
		return
	}
	j.State = Done
	j.Finished = &now
	b.summary(j, now)
}

func (b *Broker) fail(j *Job, now time.Time, why string) {
	j.State = Failed
	j.Finished = &now
	j.Error = why
	j.event(now, "selhala: %s", firstLine(why))
	if p := b.procs[j.ID]; p != nil {
		p.Kill()
	}
	b.alert("❌ <b>%s</b> · %s selhala (%s)\n<pre>%s</pre>Potřeba ruční zásah: oprav a zařaď znovu.",
		esc(j.Project), esc(j.Title), esc(stageName(j)), esc(clip(why, 1200)))
}

func (b *Broker) cancel(j *Job, now time.Time, why string) {
	j.State = Cancelled
	j.Finished = &now
	j.event(now, "%s", why)
	if p := b.procs[j.ID]; p != nil {
		p.Terminate()
	}
}

// ---------------------------------------------------------------- provisioning

func (b *Broker) switchProfile(target string, cause *Job, now time.Time) {
	from := b.st.Profile
	var cmds []string
	for a, up := range b.st.Addons {
		if up && b.cat.Addons[a].Down != "" {
			cmds = append(cmds, b.cat.Addons[a].Down)
		}
	}
	if p, ok := b.cat.Profiles[from]; ok && p.Down != "" {
		cmds = append(cmds, p.Down)
	}
	prof := b.cat.Profiles[target]
	b.busy = fmt.Sprintf("přepínám %s → %s kvůli %s", orDash(from), target, cause.ID)
	cause.event(now, "přepínám %s → %s", orDash(from), target)
	causeID := cause.ID
	work := func() {
		ctx := context.Background()
		var out string
		var err error
		for _, c := range cmds {
			if o, e := b.ex.Shell(ctx, c); e != nil {
				out += o // úklid předchozího profilu: chyba se zapíše, ale nepřeruší
			}
		}
		uctx, cancel := context.WithTimeout(ctx, prof.UpTimeout.Duration)
		o, err := b.ex.Shell(uctx, prof.Up)
		cancel()
		out += o
		b.mu.Lock()
		defer b.mu.Unlock()
		done := b.opt.Now()
		b.busy = ""
		b.st.Addons = map[string]bool{}
		if err != nil {
			b.st.Profile = ""
			b.st.Paused = true
			if j := b.find(causeID); j != nil {
				j.event(done, "přepnutí na %s selhalo", target)
			}
			b.alert("❌ přepnutí SPARKu %s → %s selhalo: %v\n<pre>%s</pre>Broker je POZASTAVENÝ, nic dalšího nespustí. Potřeba ruční zásah; pak POST /v1/resume.",
				orDash(from), target, err, esc(clip(tail0(out, 12), 1500)))
		} else {
			b.st.Profile, b.st.ProfileSince = target, done
		}
		b.saveLocked()
		b.Kick()
	}
	if b.opt.Async {
		go work()
		return
	}
	b.mu.Unlock()
	work()
	b.mu.Lock()
}

func (b *Broker) startAddons(j *Job, names []string, now time.Time) {
	for _, a := range names {
		need := b.cat.Addons[a].MemGiB + b.cat.MemReserveGiB
		if b.probe.MemAvailGiB >= 0 && b.probe.MemAvailGiB < need {
			j.Waiting = fmt.Sprintf("%s potřebuje %d GiB, volných je %d", a, need, b.probe.MemAvailGiB)
			return
		}
	}
	b.busy = fmt.Sprintf("startuji %v pro %s", names, j.ID)
	id := j.ID
	work := func() {
		var failed string
		var out string
		for _, a := range names {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
			o, err := b.ex.Shell(ctx, b.cat.Addons[a].Up)
			cancel()
			if err != nil {
				failed, out = a, o
				break
			}
			b.mu.Lock()
			b.st.Addons[a] = true
			b.mu.Unlock()
		}
		b.mu.Lock()
		defer b.mu.Unlock()
		b.busy = ""
		if failed != "" {
			if jj := b.find(id); jj != nil {
				b.fail(jj, b.opt.Now(), fmt.Sprintf("doplněk %s nenaběhl\n%s", failed, tail0(out, 10)))
			}
		}
		b.saveLocked()
		b.Kick()
	}
	if b.opt.Async {
		go work()
		return
	}
	b.mu.Unlock()
	work()
	b.mu.Lock()
}

// releaseAddons shodí doplněk, který nikdo nepoužívá a nikdo ve frontě nechce.
func (b *Broker) releaseAddons(now time.Time) {
	if !b.opt.Control {
		return
	}
	for a, up := range b.st.Addons {
		if !up {
			continue
		}
		used := false
		for _, j := range b.st.Jobs {
			if (j.Active() || j.State == Queued) && j.Stage() != nil && containsStr(j.Stage().Services, a) {
				used = true
				break
			}
		}
		if used {
			continue
		}
		b.st.Addons[a] = false
		if c := b.cat.Addons[a].Down; c != "" {
			go func(c string) {
				ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
				defer cancel()
				_, _ = b.ex.Shell(ctx, c)
			}(c)
		}
	}
}

// detect zjistí profil ze stroje: v advise pokaždé (rozvrh přepíná za nás),
// v control jen když ho neznáme (start, chyba).
func (b *Broker) detect(now time.Time) {
	if b.busy != "" || (b.opt.Control && b.st.Profile != "") || now.Sub(b.lastDetect) < b.opt.DetectEvery {
		return
	}
	b.lastDetect = now
	found := ""
	b.mu.Unlock()
	for _, n := range b.cat.ProfileOrder {
		p := b.cat.Profiles[n]
		if p.Detect == "" {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		_, err := b.ex.Shell(ctx, p.Detect)
		cancel()
		if err == nil {
			found = n
			break
		}
	}
	b.mu.Lock()
	if found != b.st.Profile {
		b.st.Profile, b.st.ProfileSince = found, now
	}
	if b.opt.Control {
		return
	}
	// advise: doplňky taky jen zjišťujeme
	b.mu.Unlock()
	up := map[string]bool{}
	for a, ad := range b.cat.Addons {
		if ad.Detect == "" {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		_, err := b.ex.Shell(ctx, ad.Detect)
		cancel()
		up[a] = err == nil
	}
	b.mu.Lock()
	b.st.Addons = up
	// rozvrh (rag-schedule) přepnul pod námi — lease, kterým služby zmizely, zpět do fronty
	for _, j := range b.st.Jobs {
		if j.State != Running || j.Stage().Mode != ModeLease {
			continue
		}
		addons, ok := b.cat.Fits(b.st.Profile, j.Stage().Services)
		for _, a := range addons {
			ok = ok && up[a]
		}
		if !ok {
			j.State = Queued
			j.event(now, "služby zmizely (SPARK přepnul na %s mimo broker), čeká znovu", orDash(b.st.Profile))
		}
	}
}

// recurring založí přechodné pevné rezervace (PromoClown, dokud neumí broker volat sám).
func (b *Broker) recurring(now time.Time) {
	m := now.Hour()*60 + now.Minute()
	for _, r := range b.cat.Recurring {
		from, _ := catalog.ParseClock(r.From)
		to, _ := catalog.ParseClock(r.To)
		var start time.Time
		day := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, now.Location())
		switch {
		case from < to && m >= from && m < to:
			start = day
		case from > to && m >= from:
			start = day
		case from > to && m < to:
			start = day.AddDate(0, 0, -1)
		default:
			continue
		}
		key := start.Format("2006-01-02")
		if b.st.RecurringDay[r.Name] == key {
			continue
		}
		b.st.RecurringDay[r.Name] = key
		end := start.Add(time.Duration(to) * time.Minute)
		if from > to {
			end = start.AddDate(0, 0, 1).Add(time.Duration(to) * time.Minute)
		}
		b.st.Seq++
		j := &Job{ID: fmt.Sprintf("%s-%d", now.Format("0102"), b.st.Seq), Project: r.Project, Title: r.Name,
			Priority: r.Priority, RequestedPriority: r.Priority, Recurring: r.Name, State: Queued, Created: now,
			Stages: []Stage{{Name: r.Name, Services: r.Services, Mode: ModeLease}}, LeaseUntil: &end}
		j.event(now, "pevná rezervace %s–%s", r.From, r.To)
		b.st.Jobs = append(b.st.Jobs, j)
	}
}

// ---------------------------------------------------------------- hlášení

func (b *Broker) summary(j *Job, now time.Time) {
	if !j.Notify || j.Recurring != "" {
		return
	}
	start := j.Created
	if j.Started != nil {
		start = *j.Started
	}
	line := fmt.Sprintf("<b>%s</b> · %s — %s%s", esc(j.Project), esc(j.Title), dur(now.Sub(start)), metricsText(j.Metrics))
	if j.Priority == 0 {
		b.st.Digest = append(b.st.Digest, line)
		return
	}
	b.note("✅ %s", line)
}

func (b *Broker) digest(now time.Time) {
	day := now.Format("2006-01-02")
	if now.Hour() < 7 || b.st.DigestDay == day {
		return
	}
	b.st.DigestDay = day
	if len(b.st.Digest) == 0 {
		return
	}
	b.note("📋 backlog za poslední den (%d úloh):\n%s", len(b.st.Digest), strings.Join(b.st.Digest, "\n"))
	b.st.Digest = nil
	// staré hotové úlohy ven ze stavu (týden)
	keep := b.st.Jobs[:0]
	for _, j := range b.st.Jobs {
		if j.Final() && j.Finished != nil && now.Sub(*j.Finished) > 7*24*time.Hour {
			continue
		}
		keep = append(keep, j)
	}
	b.st.Jobs = keep
}

func (b *Broker) note(f string, a ...any) {
	if b.nt != nil {
		b.nt.Send(fmt.Sprintf(f, a...))
	}
}

func (b *Broker) alert(f string, a ...any) { b.note(f, a...) }

func (b *Broker) saveLocked() {
	if err := save(b.opt.StatePath, b.st); err != nil {
		fmt.Fprintln(os.Stderr, "uložení stavu selhalo:", err)
	}
}

func (b *Broker) find(id string) *Job {
	for _, j := range b.st.Jobs {
		if j.ID == id {
			return j
		}
	}
	return nil
}
