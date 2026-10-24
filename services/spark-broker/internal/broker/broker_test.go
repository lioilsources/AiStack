package broker

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/catalog"
)

const testCatalog = `
profile_order: [comfy, llm, director, gptoss]
profiles:
  comfy:    {services: [comfyui, flux-schnell], up: "up comfy", detect: "is comfy"}
  llm:      {services: [qwen36, flux-schnell], up: "up llm", detect: "is llm"}
  director: {services: [director], up: "up director", detect: "is director"}
  gptoss:   {services: [gptoss], up: "up gptoss", down: "rm gptoss", detect: "is gptoss"}
addons:
  audio-music: {profiles: [comfy], up: "start music", down: "stop music", detect: "is music", mem_gib: 10, max_jobs: 1}
projects:
  storyteller: {max: 50}
  webtoon: {max: 30}
default_project_max: 20
recurring:
  - {name: promoclown, project: promoclown, services: [qwen36], from: "17:05", to: "00:55", priority: 90}
`

type fakeProc struct {
	ch      chan int
	metrics map[string]Count
	termed  bool
	mu      sync.Mutex
}

func (p *fakeProc) Wait() (int, map[string]Count) { c := <-p.ch; return c, p.metrics }
func (p *fakeProc) Terminate() {
	p.mu.Lock()
	p.termed = true
	p.mu.Unlock()
}
func (p *fakeProc) Kill() {}

type fakeEx struct {
	mu    sync.Mutex
	cmds  []string
	fail  map[string]bool
	ok    map[string]bool // detect: které příkazy uspějí
	procs []*fakeProc
	probe Probe
}

func (f *fakeEx) Shell(_ context.Context, cmd string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cmds = append(f.cmds, cmd)
	if f.fail[cmd] {
		return "boom\nCUDA out of memory", errors.New("exit 1")
	}
	if strings.HasPrefix(cmd, "is ") && !f.ok[cmd] {
		return "", errors.New("no")
	}
	return "", nil
}

func (f *fakeEx) Start(cmd, dir, log string, env []string) (Proc, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cmds = append(f.cmds, "run "+cmd)
	p := &fakeProc{ch: make(chan int, 1)}
	f.procs = append(f.procs, p)
	return p, nil
}

func (f *fakeEx) Probe(context.Context) Probe { return f.probe }

func (f *fakeEx) ran(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.cmds {
		if strings.HasPrefix(c, prefix) {
			n++
		}
	}
	return n
}

type fakeNotify struct {
	mu  sync.Mutex
	msg []string
}

func (n *fakeNotify) Send(t string) { n.mu.Lock(); n.msg = append(n.msg, t); n.mu.Unlock() }
func (n *fakeNotify) has(s string) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	for _, m := range n.msg {
		if strings.Contains(m, s) {
			return true
		}
	}
	return false
}

type env struct {
	t   *testing.T
	b   *Broker
	ex  *fakeEx
	nt  *fakeNotify
	now time.Time
}

func newEnv(t *testing.T, control bool, profile string) *env {
	t.Helper()
	cat, err := catalog.Parse([]byte(testCatalog))
	if err != nil {
		t.Fatal(err)
	}
	cat.LogDir = t.TempDir()
	e := &env{t: t, ex: &fakeEx{fail: map[string]bool{}, ok: map[string]bool{}, probe: Probe{MemAvailGiB: 80, GPUTempC: 60, ComfyActive: -1}},
		nt: &fakeNotify{}, now: time.Date(2026, 10, 23, 9, 0, 0, 0, time.Local)}
	e.b, err = New(cat, e.ex, e.nt, Options{Control: control, Now: func() time.Time { return e.now }})
	if err != nil {
		t.Fatal(err)
	}
	e.b.st.Profile = profile
	e.b.probe = e.ex.probe
	return e
}

func (e *env) tick()               { e.b.Tick() }
func (e *env) adv(d time.Duration) { e.now = e.now.Add(d); e.b.Tick() }
func (e *env) submit(r SubmitReq) *Job {
	e.t.Helper()
	j, err := e.b.Submit(r)
	if err != nil {
		e.t.Fatal(err)
	}
	return j
}
func (e *env) state(id string) *Job {
	j, err := e.b.Get(id)
	if err != nil {
		e.t.Fatal(err)
	}
	return j
}
func (e *env) want(id, state string) {
	e.t.Helper()
	if j := e.state(id); j.State != state {
		e.t.Fatalf("%s: stav %s, čekal jsem %s (waiting=%q, events=%v)", id, j.State, state, j.Waiting, j.Events)
	}
}

func lease(project string, prio int, svcs ...string) SubmitReq {
	return SubmitReq{Project: project, Title: project + " práce", Priority: prio,
		Stages: []Stage{{Services: svcs, Mode: ModeLease, LeaseMin: 60}}}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	for i := 0; i < 200; i++ {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("podmínka nenastala")
}

func TestLeaseOnCurrentProfileStartsWithoutSwitch(t *testing.T) {
	e := newEnv(t, true, "comfy")
	j := e.submit(lease("webtoon", 20, "comfyui"))
	e.tick()
	e.want(j.ID, Running)
	if e.ex.ran("up ") != 0 {
		t.Fatal("nemělo se přepínat")
	}
}

func TestSwitchWhenIdle(t *testing.T) {
	e := newEnv(t, true, "comfy")
	j := e.submit(lease("storyteller", 30, "director"))
	e.tick() // přepnutí
	e.tick() // start
	e.want(j.ID, Running)
	if e.b.Status().Profile != "director" || e.ex.ran("up director") != 1 {
		t.Fatalf("profil %s", e.b.Status().Profile)
	}
}

// Vyšší priorita odsune nižší preemptible lease; po ní se nižší vrátí.
func TestPreemptLeaseAndResume(t *testing.T) {
	e := newEnv(t, true, "comfy")
	low := e.submit(lease("webtoon", 20, "comfyui"))
	e.tick()
	e.want(low.ID, Running)
	high := e.submit(lease("storyteller", 40, "director"))
	e.tick()
	e.want(low.ID, Draining)
	e.want(high.ID, Queued)
	e.adv(6 * time.Minute) // DrainGrace 5 min → low zpět do fronty, přepnutí
	e.want(low.ID, Queued)
	e.tick()
	e.want(high.ID, Running)
	if _, err := e.b.Done(high.ID, DoneReq{Metrics: map[string]Count{"chunks": {OK: 120, Failed: 2}}}); err != nil {
		t.Fatal(err)
	}
	e.tick() // přepnutí zpět na comfy
	e.tick()
	e.want(low.ID, Running)
	if !e.nt.has("chunks 120 (2 chyb)") {
		t.Fatalf("souhrn chybí: %v", e.nt.msg)
	}
}

// Webtoon 24. 10.: překlad → ComfyUI. Navazující úloha je ve frontě hned, ale čeká na předchozí;
// nižší úloha bez závislosti mezitím jet smí.
func TestAfterWaitsForDependency(t *testing.T) {
	e := newEnv(t, true, "llm")
	first := e.submit(lease("webtoon", 30, "qwen36"))
	e.tick()
	e.want(first.ID, Running)
	next := e.submit(SubmitReq{Project: "webtoon", Priority: 30, After: []string{first.ID},
		Stages: []Stage{{Services: []string{"comfyui"}, Mode: ModeLease, LeaseMin: 60}}})
	e.tick()
	e.want(next.ID, Queued)
	if got := e.state(next.ID).Waiting; !strings.HasPrefix(got, "po "+first.ID) {
		t.Fatalf("waiting = %q", got)
	}
	if e.b.Status().Profile != "llm" {
		t.Fatal("závislá úloha nesmí přepnout profil")
	}
	if _, err := e.b.Done(first.ID, DoneReq{}); err != nil {
		t.Fatal(err)
	}
	e.tick()
	e.tick()
	e.want(next.ID, Running)
	if _, err := e.b.Submit(SubmitReq{Project: "x", After: []string{"nope"}, Stages: []Stage{{Services: []string{"comfyui"}}}}); err == nil {
		t.Fatal("after na neexistující úlohu nesmí projít")
	}
}

// Okno uživatele (on_behalf_of_user) se neodsouvá; projekt čeká s důvodem.
func TestUserWindowBlocksAndIsAnnounced(t *testing.T) {
	e := newEnv(t, true, "comfy")
	w := e.submit(SubmitReq{Project: "user", OnBehalfOfUser: true, UserQuote: "chci comfy na 3 hodiny",
		Stages: []Stage{{Services: []string{"comfyui"}, LeaseMin: 180}}})
	if w.Priority != 100 || w.Preemptible || w.Heartbeat {
		t.Fatalf("okno uživatele: %+v", w)
	}
	if !e.nt.has("chci comfy na 3 hodiny") {
		t.Fatal("okno jménem uživatele se musí ohlásit")
	}
	e.tick()
	e.want(w.ID, Running)
	j := e.submit(lease("storyteller", 50, "director"))
	e.tick()
	e.want(j.ID, Queued)
	if got := e.state(j.ID).Waiting; !strings.Contains(got, "priorita 100") {
		t.Fatalf("waiting = %q", got)
	}
	if _, err := e.b.Submit(SubmitReq{Project: "x", OnBehalfOfUser: true, Stages: []Stage{{Services: []string{"comfyui"}}}}); err == nil {
		t.Fatal("bez user_quote nesmí projít")
	}
}

// Webtoon 23. 10.: session umřela na usage limit uprostřed lease.
func TestOrphanedLeaseReleasesSpark(t *testing.T) {
	e := newEnv(t, true, "comfy")
	j := e.submit(lease("webtoon", 20, "comfyui"))
	e.tick()
	e.adv(5 * time.Minute)
	e.b.Heartbeat(j.ID, nil)
	e.adv(9 * time.Minute)
	e.want(j.ID, Running)
	e.adv(2 * time.Minute)
	e.want(j.ID, Orphaned)
	if !e.nt.has("heartbeat") {
		t.Fatal("sirotek se musí hlásit")
	}
}

func TestIdleUserWindowClosesAfterWarning(t *testing.T) {
	e := newEnv(t, true, "comfy")
	e.ex.probe.ComfyActive = 5
	e.b.probe = e.ex.probe
	w := e.submit(SubmitReq{Project: "user", OnBehalfOfUser: true, UserQuote: "comfy", Stages: []Stage{{Services: []string{"comfyui"}, LeaseMin: 240}}})
	e.tick()
	e.adv(20 * time.Minute)
	e.b.probe.ComfyActive = 6 // uživatel něco vyrenderoval
	e.adv(time.Minute)
	e.adv(29 * time.Minute)
	e.want(w.ID, Running)
	e.adv(2 * time.Minute)
	if !e.nt.has("nečinné") {
		t.Fatal("chybí varování o nečinnosti")
	}
	e.adv(16 * time.Minute)
	e.want(w.ID, Done)
}

func TestPriorityAboveProjectMaxNeedsApproval(t *testing.T) {
	e := newEnv(t, true, "comfy")
	j := e.submit(lease("webtoon", 80, "comfyui"))
	if j.Priority != 30 || j.PendingPriority != 80 {
		t.Fatalf("priorita %d pending %d", j.Priority, j.PendingPriority)
	}
	if !e.nt.has("chce prioritu 80") {
		t.Fatal("chybí dotaz na uživatele")
	}
	j, _ = e.b.Approve(j.ID)
	if j.Priority != 80 {
		t.Fatal("schválení neplatí")
	}
}

func TestRunStagesAcrossProfilesWithMetrics(t *testing.T) {
	e := newEnv(t, true, "comfy")
	j := e.submit(SubmitReq{Project: "storyteller", Priority: 30, Resume: true, Stages: []Stage{
		{Name: "vote", Services: []string{"director"}, Mode: ModeRun, Command: "vote.sh"},
		{Name: "gen", Services: []string{"gptoss"}, Mode: ModeRun, Command: "gen.sh"},
	}})
	e.tick()
	e.tick()
	e.want(j.ID, Running)
	e.ex.procs[0].metrics = map[string]Count{"votes": {OK: 4897}}
	e.ex.procs[0].ch <- 0
	waitFor(t, func() bool { return e.state(j.ID).State == Queued })
	e.tick()
	e.tick()
	e.want(j.ID, Running)
	if e.b.Status().Profile != "gptoss" {
		t.Fatal("druhá stage potřebuje gptoss")
	}
	e.ex.procs[1].metrics = map[string]Count{"motifs": {OK: 600, Failed: 25}}
	e.ex.procs[1].ch <- 0
	waitFor(t, func() bool { return e.state(j.ID).State == Done })
	if !e.nt.has("motifs 600 (25 chyb) · votes 4897") {
		t.Fatalf("souhrn: %v", e.nt.msg)
	}
	// odchod z gptoss uklidí bench kontejner
	e.submit(lease("webtoon", 20, "comfyui"))
	e.tick()
	if e.ex.ran("rm gptoss") != 1 {
		t.Fatal("down gptoss se nespustil")
	}
}

func TestRunFailureAlertsImmediately(t *testing.T) {
	e := newEnv(t, true, "director")
	j := e.submit(SubmitReq{Project: "storyteller", Priority: 30, Preemptible: ptr(false),
		Stages: []Stage{{Services: []string{"director"}, Mode: ModeRun, Command: "x.sh"}}})
	e.tick()
	e.ex.procs[0].ch <- 2
	waitFor(t, func() bool { return e.state(j.ID).State == Failed })
	if !e.nt.has("ruční zásah") {
		t.Fatal("chyba se musí hlásit hned")
	}
}

// 23. 10. 08:03: přepnutí, které se pokazí, nesmí běžet dál naslepo.
func TestSwitchFailurePausesBroker(t *testing.T) {
	e := newEnv(t, true, "comfy")
	e.ex.fail["up director"] = true
	j := e.submit(lease("storyteller", 30, "director"))
	e.tick()
	if !e.b.Status().Paused || !e.nt.has("POZASTAVENÝ") {
		t.Fatal("broker se musí zastavit a ohlásit")
	}
	e.tick()
	e.want(j.ID, Queued)
	if e.ex.ran("up director") != 1 {
		t.Fatal("nesmí zkoušet znovu, dokud uživatel nepustí resume")
	}
}

func TestAddonStartsAndStopsWhenUnused(t *testing.T) {
	e := newEnv(t, true, "comfy")
	a := e.submit(lease("webtoon", 20, "audio-music"))
	b := e.submit(lease("kirian", 20, "audio-music"))
	e.tick() // start doplňku
	e.tick()
	e.want(a.ID, Running)
	e.want(b.ID, Queued) // max_jobs 1
	e.b.Done(a.ID, DoneReq{Metrics: map[string]Count{"sounds": {OK: 15}}})
	e.tick()
	e.want(b.ID, Running)
	e.b.Done(b.ID, DoneReq{})
	e.tick()
	waitFor(t, func() bool { return e.ex.ran("stop music") == 1 })
	if e.ex.ran("start music") != 1 {
		t.Fatal("doplněk se startoval víckrát")
	}
}

func TestAddonNeedsMemory(t *testing.T) {
	e := newEnv(t, true, "comfy")
	e.b.probe.MemAvailGiB = 12
	j := e.submit(lease("webtoon", 20, "audio-music"))
	e.tick()
	if w := e.state(j.ID).Waiting; !strings.Contains(w, "GiB") {
		t.Fatalf("waiting = %q", w)
	}
}

func TestRecurringOverMidnight(t *testing.T) {
	e := newEnv(t, true, "comfy")
	e.now = time.Date(2026, 10, 24, 0, 30, 0, 0, time.Local)
	e.tick()
	jobs := e.b.List("promoclown", true)
	if len(jobs) != 1 || jobs[0].LeaseUntil.Hour() != 0 || jobs[0].LeaseUntil.Minute() != 55 {
		t.Fatalf("rezervace: %+v", jobs)
	}
	e.tick()
	e.tick()
	if e.b.List("promoclown", true)[0].State != Running {
		t.Fatal("PromoClown nedostal qwen36")
	}
	e.adv(30 * time.Minute)
	if e.b.List("promoclown", true)[0].State != Done {
		t.Fatal("rezervace měla skončit v 00:55")
	}
	e.adv(time.Hour)
	if len(e.b.List("promoclown", true)) != 1 {
		t.Fatal("rezervace se nesmí založit dvakrát")
	}
}

func TestSamePriorityPrefersNoSwitch(t *testing.T) {
	e := newEnv(t, true, "comfy")
	d := e.submit(lease("storyteller", 20, "director"))
	c := e.submit(lease("webtoon", 20, "comfyui"))
	e.tick()
	e.want(c.ID, Running)
	e.want(d.ID, Queued)
}

func TestAdviseNeverProvisions(t *testing.T) {
	e := newEnv(t, false, "")
	e.ex.ok["is llm"] = true
	l := e.submit(lease("toyshaders", 20, "qwen36"))
	c := e.submit(lease("webtoon", 20, "comfyui"))
	r := e.submit(SubmitReq{Project: "storyteller", Priority: 20, Preemptible: ptr(false), Stages: []Stage{{Services: []string{"qwen36"}, Mode: ModeRun, Command: "x"}}})
	e.tick()
	e.want(l.ID, Running)
	e.want(c.ID, Queued)
	e.want(r.ID, Queued)
	if e.ex.ran("up ") != 0 || e.ex.ran("run ") != 0 {
		t.Fatalf("advise nesmí nic spouštět: %v", e.ex.cmds)
	}
}

func TestValidation(t *testing.T) {
	e := newEnv(t, true, "comfy")
	for name, r := range map[string]SubmitReq{
		"preemptible run bez resume": {Project: "a", Stages: []Stage{{Services: []string{"director"}, Mode: ModeRun, Command: "x"}}},
		"neslučitelné služby":        {Project: "a", Stages: []Stage{{Services: []string{"comfyui", "director"}}}},
		"neznámá služba":             {Project: "a", Stages: []Stage{{Services: []string{"sdxl-magic"}}}},
		"bez projektu":               {Stages: []Stage{{Services: []string{"comfyui"}}}},
	} {
		if _, err := e.b.Submit(r); err == nil {
			t.Errorf("%s: mělo selhat", name)
		}
	}
}

func TestThermalPause(t *testing.T) {
	e := newEnv(t, true, "comfy")
	e.b.probe.GPUTempC = 92
	j := e.submit(lease("webtoon", 20, "comfyui"))
	e.tick()
	e.want(j.ID, Queued)
	e.adv(11 * time.Minute)
	if !e.nt.has("°C") {
		t.Fatal("teplota se má hlásit")
	}
}

func ptr[T any](v T) *T { return &v }

// advise: rag-schedule.timer ve 13:00 přepne comfy → llm pod rukama.
func TestAdviseRequeuesWhenScheduleSwitches(t *testing.T) {
	e := newEnv(t, false, "")
	e.ex.ok["is comfy"] = true
	j := e.submit(lease("chrestys", 20, "comfyui"))
	e.tick()
	e.want(j.ID, Running)
	e.ex.ok["is comfy"], e.ex.ok["is llm"] = false, true
	e.adv(2 * time.Minute)
	e.want(j.ID, Queued)
	if w := e.state(j.ID).Waiting; !strings.Contains(w, "comfy") {
		t.Fatalf("waiting = %q", w)
	}
}
