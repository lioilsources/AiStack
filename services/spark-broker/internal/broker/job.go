package broker

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// Stavy úlohy.
const (
	Queued    = "queued"   // čeká na prostředí
	Running   = "running"  // prostředí je připravené; lease: session volá služby, run: příkaz běží
	Draining  = "draining" // dostala pokyn uvolnit SPARK (preempce), do DrainAt musí skončit
	Done      = "done"
	Failed    = "failed"
	Cancelled = "cancelled"
	Orphaned  = "orphaned" // lease bez heartbeatu — session nejspíš umřela (usage limit)
)

// Režimy stage.
const (
	ModeRun   = "run"   // příkaz spouští broker na SPARKu
	ModeLease = "lease" // broker jen připraví prostředí, session volá HTTP sama
)

type Stage struct {
	Name        string   `json:"name"`
	Services    []string `json:"services"`
	Mode        string   `json:"mode"`
	Command     string   `json:"command,omitempty"` // run: bash -lc na SPARKu
	Dir         string   `json:"dir,omitempty"`
	EstimateMin int      `json:"estimate_min,omitempty"`
	LeaseMin    int      `json:"lease_min,omitempty"` // lease: jak dlouho prostředí drží (prodloužit přes /extend)
}

type Count struct {
	OK     int `json:"ok"`
	Failed int `json:"failed"`
}

type Event struct {
	At   time.Time `json:"at"`
	Text string    `json:"text"`
}

type Job struct {
	ID       string `json:"id"`
	Project  string `json:"project"`
	Session  string `json:"session,omitempty"`
	Title    string `json:"title"`
	Priority int    `json:"priority"`
	// Kolik si úloha řekla; Priority je to, co platí (strop projektu, schválení uživatelem).
	RequestedPriority int `json:"requested_priority"`
	// Úloha zadaná jménem uživatele (okno na vyžádání) — smí na prioritu 100,
	// nepotřebuje heartbeat, zavírá se při nečinnosti; Telegram to vždy ohlásí.
	OnBehalfOfUser bool   `json:"on_behalf_of_user,omitempty"`
	UserQuote      string `json:"user_quote,omitempty"`
	// Priorita nad strop projektu, která čeká na souhlas uživatele; do té doby platí Priority.
	PendingPriority int `json:"pending_priority,omitempty"`

	Deadline *time.Time `json:"deadline,omitempty"`
	// Jednorázová akce na čas: do té doby jen čeká ve frontě (vidět v ls). Když čas
	// nastane, předbíhá dlouhé úlohy, které se dají přerušit a navázat (viz preemptsLong).
	NotBefore    *time.Time `json:"not_before,omitempty"`
	Preemptible  bool       `json:"preemptible"`
	Resume       bool       `json:"resume"` // run: po SIGTERM jde bezpečně pustit znovu
	Heartbeat    bool       `json:"heartbeat"`
	IdleCloseMin int        `json:"idle_close_min,omitempty"` // lease: zavřít po N min bez aktivity v ComfyUI
	Recurring    string     `json:"recurring,omitempty"`
	Notify       bool       `json:"notify"` // souhrn po skončení (chyby se hlásí vždy)

	After    []string `json:"after,omitempty"` // startuje až po skončení těchto úloh
	Stages   []Stage  `json:"stages"`
	StageIdx int      `json:"stage_idx"`

	State         string           `json:"state"`
	Created       time.Time        `json:"created"`
	Started       *time.Time       `json:"started,omitempty"`
	StageStarted  *time.Time       `json:"stage_started,omitempty"`
	Finished      *time.Time       `json:"finished,omitempty"`
	LeaseUntil    *time.Time       `json:"lease_until,omitempty"`
	LastHeartbeat *time.Time       `json:"last_heartbeat,omitempty"`
	LastActivity  *time.Time       `json:"last_activity,omitempty"`
	IdleWarned    bool             `json:"idle_warned,omitempty"`
	DrainAt       *time.Time       `json:"drain_at,omitempty"`
	Profile       string           `json:"profile,omitempty"`
	Metrics       map[string]Count `json:"metrics,omitempty"`
	Error         string           `json:"error,omitempty"`
	Log           string           `json:"log,omitempty"`
	Events        []Event          `json:"events,omitempty"`
	Attempts      int              `json:"attempts,omitempty"`
	Waiting       string           `json:"waiting,omitempty"` // proč ještě neběží (plní plánovač)
	Position      int              `json:"position,omitempty"`
}

func (j *Job) Stage() *Stage {
	if j.StageIdx < len(j.Stages) {
		return &j.Stages[j.StageIdx]
	}
	return nil
}

func (j *Job) Active() bool { return j.State == Running || j.State == Draining }
func (j *Job) Final() bool {
	return j.State == Done || j.State == Failed || j.State == Cancelled || j.State == Orphaned
}

func (j *Job) event(now time.Time, f string, a ...any) {
	j.Events = append(j.Events, Event{At: now, Text: fmt.Sprintf(f, a...)})
	if len(j.Events) > 50 {
		j.Events = j.Events[len(j.Events)-50:]
	}
}

func (j *Job) addMetrics(m map[string]Count) {
	if len(m) == 0 {
		return
	}
	if j.Metrics == nil {
		j.Metrics = map[string]Count{}
	}
	for k, v := range m {
		c := j.Metrics[k]
		c.OK += v.OK
		c.Failed += v.Failed
		j.Metrics[k] = c
	}
}

// State se ukládá jako JSON (atomický zápis). Úloh je desítky, ne miliony;
// SQLite by chtělo cgo nebo velkou závislost a nic by nepřineslo.
type persisted struct {
	Profile      string            `json:"profile"`
	ProfileSince time.Time         `json:"profile_since"`
	Addons       map[string]bool   `json:"addons"`
	Paused       bool              `json:"paused"`
	Seq          int               `json:"seq"`
	Jobs         []*Job            `json:"jobs"`
	RecurringDay map[string]string `json:"recurring_day"` // jméno → den, kdy už rezervace vznikla
	Digest       []string          `json:"digest"`        // souhrny backlogu do denního hlášení
	DigestDay    string            `json:"digest_day"`
}

func save(path string, p *persisted) error {
	if path == "" {
		return nil
	}
	b, err := json.MarshalIndent(p, "", " ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func load(path string) (*persisted, error) {
	p := &persisted{Addons: map[string]bool{}, RecurringDay: map[string]string{}}
	if path == "" {
		return p, nil
	}
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return p, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(b, p); err != nil {
		return nil, err
	}
	if p.Addons == nil {
		p.Addons = map[string]bool{}
	}
	if p.RecurringDay == nil {
		p.RecurringDay = map[string]string{}
	}
	return p, nil
}

// due: jednorázová akce na čas, jejíž čas už nastal.
func (j *Job) due(now time.Time) bool {
	return j.NotBefore != nil && !now.Before(*j.NotBefore)
}
