package broker

import (
	"encoding/json"
	"fmt"
	"html"
	"os"
	"sort"
	"strings"
	"time"
)

func clone(j *Job) *Job {
	b, _ := json.Marshal(j)
	var c Job
	_ = json.Unmarshal(b, &c)
	return &c
}

// slim: kopie bez historie událostí (do /v1/status).
func slim(j *Job) *Job {
	c := clone(j)
	c.Events = nil
	return c
}

func usesComfy(j *Job) bool {
	st := j.Stage()
	return st != nil && containsStr(st.Services, "comfyui")
}

func stagesLabel(j *Job) string {
	st := j.Stage()
	if st == nil && len(j.Stages) > 0 {
		st = &j.Stages[len(j.Stages)-1]
	}
	if st == nil {
		return "-"
	}
	return strings.Join(st.Services, "+")
}

func stageName(j *Job) string {
	if st := j.Stage(); st != nil {
		return st.Name
	}
	return "-"
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func esc(s string) string { return html.EscapeString(s) }

func containsStr(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}

// tail: posledních n řádků souboru.
func tail(path string, n int) string {
	if path == "" {
		return ""
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return tail0(string(b), n)
}

func tail0(s string, n int) string {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

func dur(d time.Duration) string {
	if d < time.Minute {
		return fmt.Sprintf("%d s", int(d.Seconds()))
	}
	if d < time.Hour {
		return fmt.Sprintf("%d min", int(d.Minutes()))
	}
	return fmt.Sprintf("%d h %02d min", int(d.Hours()), int(d.Minutes())%60)
}

func metricsText(m map[string]Count) string {
	if len(m) == 0 {
		return ""
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		c := m[k]
		p := fmt.Sprintf("%s %d", esc(k), c.OK)
		if c.Failed > 0 {
			p += fmt.Sprintf(" (%d chyb)", c.Failed)
		}
		parts = append(parts, p)
	}
	return "\n" + strings.Join(parts, " · ")
}

// clock: čas pro lidi — dnes jen hodiny, jindy i den.
func clock(t, now time.Time) string {
	if t.Format("2006-01-02") == now.Format("2006-01-02") {
		return t.Format("15:04")
	}
	return t.Format("2. 1. 15:04")
}
