package comfyq

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Window decision (PLAN-image-api.md fáze 0): the source of truth is ComfyUI
// itself. rag-schedule.sh starts the comfyui unit only in the comfy profile
// and stops it in every other one, manual switches included, so "ComfyUI
// answers with a CUDA device" == "the comfy window is open". The SCHEDULE
// string (same as the rag-schedule drop-in) is used only to stop starting new
// jobs shortly before a planned switch away from comfy and to tell clients
// when the next window opens.

type Gate struct {
	Comfy       *Client
	Schedule    []Slot
	Loc         *time.Location
	Cutoff      time.Duration // no new job this close to a switch away from comfy
	ThermalCSV  string
	MaxGPUC     float64
	MaxZoneC    float64
	Now         func() time.Time
	CacheTTL    time.Duration
	mu          sync.Mutex
	cached      GateState
	cachedUntil time.Time
}

type Slot struct {
	Hour    int
	Profile string
}

type GateState struct {
	// open | closed | busy | hot | cpu
	State    string     `json:"state"`
	Reason   string     `json:"reason,omitempty"`
	ComfyUp  bool       `json:"comfyui_up"`
	NextOpen *time.Time `json:"next_open,omitempty"`
	// Next planned switch away from comfy while the window is open.
	Closes  *time.Time `json:"closes,omitempty"`
	GPUC    float64    `json:"gpu_temp_c,omitempty"`
	ZoneC   float64    `json:"zone_max_c,omitempty"`
	Checked time.Time  `json:"checked"`
}

func (g GateState) Open() bool { return g.State == "open" }

// ParseSchedule reads "7:comfy 13:llm" (hour:profile, local time).
func ParseSchedule(s string) ([]Slot, error) {
	var out []Slot
	for _, f := range strings.Fields(s) {
		h, p, ok := strings.Cut(f, ":")
		n, err := strconv.Atoi(h)
		if !ok || err != nil || n < 0 || n > 23 || p == "" {
			return nil, fmt.Errorf("bad schedule entry %q", f)
		}
		out = append(out, Slot{Hour: n, Profile: p})
	}
	return out, nil
}

// profileAt returns the scheduled profile at t and the next switch time.
func (g *Gate) profileAt(t time.Time) (string, time.Time, bool) {
	if len(g.Schedule) == 0 {
		return "", time.Time{}, false
	}
	t = t.In(g.Loc)
	day := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, g.Loc)
	// Walk switch points from yesterday to tomorrow.
	type pt struct {
		at time.Time
		p  string
	}
	var pts []pt
	for d := -1; d <= 1; d++ {
		for _, s := range g.Schedule {
			pts = append(pts, pt{day.AddDate(0, 0, d).Add(time.Duration(s.Hour) * time.Hour), s.Profile})
		}
	}
	cur, next := "", time.Time{}
	var last time.Time
	for _, p := range pts {
		if !p.at.After(t) && p.at.After(last) {
			last, cur = p.at, p.p
		}
	}
	for _, p := range pts {
		if p.at.After(t) && p.p != cur && (next.IsZero() || p.at.Before(next)) {
			next = p.at
		}
	}
	return cur, next, true
}

// nextComfy returns when the schedule next switches to comfy after t.
func (g *Gate) nextComfy(t time.Time) time.Time {
	t = t.In(g.Loc)
	day := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, g.Loc)
	var best time.Time
	for d := 0; d <= 1; d++ {
		for _, s := range g.Schedule {
			at := day.AddDate(0, 0, d).Add(time.Duration(s.Hour) * time.Hour)
			if s.Profile == "comfy" && at.After(t) && (best.IsZero() || at.Before(best)) {
				best = at
			}
		}
	}
	return best
}

func (g *Gate) now() time.Time {
	if g.Now != nil {
		return g.Now()
	}
	return time.Now()
}

// Check returns the gate state, cached for a few seconds so /status polling
// by many clients does not hammer ComfyUI.
func (g *Gate) Check(ctx context.Context) GateState {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := g.now()
	if now.Before(g.cachedUntil) {
		return g.cached
	}
	st := g.check(ctx, now)
	g.cached, g.cachedUntil = st, now.Add(g.CacheTTL)
	return st
}

func (g *Gate) Invalidate() {
	g.mu.Lock()
	g.cachedUntil = time.Time{}
	g.mu.Unlock()
}

func (g *Gate) check(ctx context.Context, now time.Time) GateState {
	st := GateState{Checked: now}
	if len(g.Schedule) > 0 {
		if n := g.nextComfy(now); !n.IsZero() {
			st.NextOpen = &n
		}
	}
	stats, err := g.Comfy.SystemStats(ctx)
	if err != nil {
		st.State, st.Reason = "closed", "ComfyUI neběží (mimo okno comfy)"
		return st
	}
	st.ComfyUp = true
	if !stats.hasCUDA() {
		st.State, st.Reason = "cpu", "ComfyUI nevidí CUDA zařízení, render by šel na CPU"
		return st
	}
	if cur, next, ok := g.profileAt(now); ok && cur == "comfy" && !next.IsZero() {
		st.Closes = &next
		if next.Sub(now) < g.Cutoff {
			st.State, st.Reason = "closed", fmt.Sprintf("okno comfy končí v %s", next.In(g.Loc).Format("15:04"))
			return st
		}
	}
	if g.ThermalCSV != "" {
		if gpu, zone, ok := readThermal(g.ThermalCSV, now); ok {
			st.GPUC, st.ZoneC = gpu, zone
			if gpu >= g.MaxGPUC || zone >= g.MaxZoneC {
				st.State, st.Reason = "hot", fmt.Sprintf("GPU %.0f °C, zóna %.0f °C", gpu, zone)
				return st
			}
		}
	}
	running, pending, err := g.Comfy.QueueLen(ctx)
	if err != nil {
		st.State, st.Reason = "closed", "ComfyUI /queue neodpovídá"
		return st
	}
	if running+pending > 0 {
		st.State, st.Reason = "busy", fmt.Sprintf("ComfyUI má cizí práci (%d běží, %d čeká)", running, pending)
		return st
	}
	st.State = "open"
	return st
}

// readThermal reads the last line of ~/ops/thermal.csv (ts,gpu_temp_c,...,
// zone_max_c at index 8). A line older than 2 minutes is ignored: a dead
// logger must not block the queue forever.
func readThermal(path string, now time.Time) (gpu, zone float64, ok bool) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, false
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil || now.Sub(fi.ModTime()) > 2*time.Minute {
		return 0, 0, false
	}
	size := fi.Size()
	off := size - 1024
	if off < 0 {
		off = 0
	}
	buf := make([]byte, size-off)
	if _, err := f.ReadAt(buf, off); err != nil {
		return 0, 0, false
	}
	lines := strings.Split(strings.TrimSpace(string(buf)), "\n")
	cols := strings.Split(lines[len(lines)-1], ",")
	if len(cols) < 9 {
		return 0, 0, false
	}
	gpu, e1 := strconv.ParseFloat(cols[1], 64)
	zone, e2 := strconv.ParseFloat(cols[8], 64)
	if e1 != nil || e2 != nil {
		return 0, 0, false
	}
	return gpu, zone, true
}

type systemStats struct {
	Devices []struct {
		Type string `json:"type"`
	} `json:"devices"`
}

func (s systemStats) hasCUDA() bool {
	for _, d := range s.Devices {
		if d.Type == "cuda" {
			return true
		}
	}
	return false
}
