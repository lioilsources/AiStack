package main

import (
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
)

// overview je `sparkctl ls`: hlavička stroje, živé úlohy (běžící napřed, pak podle
// priority) a posledních pět skončených.
func overview(w io.Writer, st broker.Status, jobs []*broker.Job, now time.Time) {
	head := fmt.Sprintf("SPARK %s: profil %s", st.Mode, st.Profile)
	if len(st.Addons) > 0 {
		head += " + " + strings.Join(st.Addons, ",")
	}
	if st.Busy != "" {
		head += " (" + st.Busy + ")"
	}
	head += fmt.Sprintf(" · volno %d GiB · GPU %d °C %d %%", st.Probe.MemAvailGiB, st.Probe.GPUTempC, st.Probe.GPUUtil)
	if st.Paused {
		head += " · POZASTAVENO"
	}
	fmt.Fprintln(w, head)

	var live, ended []*broker.Job
	for _, j := range jobs {
		if j.Final() {
			ended = append(ended, j)
		} else {
			live = append(live, j)
		}
	}
	if len(live) == 0 {
		fmt.Fprintln(w, "fronta prázdná")
	}
	sort.SliceStable(live, func(a, b int) bool {
		ra, rb := live[a].State != broker.Queued, live[b].State != broker.Queued
		if ra != rb {
			return ra
		}
		if live[a].Priority != live[b].Priority {
			return live[a].Priority > live[b].Priority
		}
		return live[a].Created.Before(live[b].Created)
	})
	for _, j := range live {
		var info string
		switch {
		case j.State == broker.Queued:
			info = "čeká: " + j.Waiting
		case j.LeaseUntil != nil:
			info = "zbývá " + span(j.LeaseUntil.Sub(now))
		case j.StageStarted != nil:
			info = "běží " + span(now.Sub(*j.StageStarted))
		}
		prio := fmt.Sprintf("p%d", j.Priority)
		if j.PendingPriority > 0 {
			prio += fmt.Sprintf("→%d?", j.PendingPriority)
		}
		var svcs []string
		if s := stageOf(j); s != nil {
			svcs = s.Services
		}
		fmt.Fprintf(w, "  %-8s %-8s %-6s %s: %s [%s] %d/%d · %s\n", j.State, j.ID, prio, j.Project, j.Title,
			strings.Join(svcs, ","), min(j.StageIdx+1, len(j.Stages)), len(j.Stages), info)
	}

	if len(ended) > 5 {
		ended = ended[len(ended)-5:]
	}
	if len(ended) > 0 {
		fmt.Fprintln(w, "naposledy skončené:")
	}
	for _, j := range ended {
		line := fmt.Sprintf("  %-9s %-8s %s: %s", j.State, j.ID, j.Project, j.Title)
		if j.Finished != nil {
			line += " (" + j.Finished.Local().Format("15:04") + ")"
		}
		if j.Error != "" {
			line += " — " + j.Error
		}
		fmt.Fprintln(w, line)
	}
}

// stageOf vrátí aktuální stage, u hotové úlohy poslední.
func stageOf(j *broker.Job) *broker.Stage {
	if len(j.Stages) == 0 {
		return nil
	}
	return &j.Stages[min(j.StageIdx, len(j.Stages)-1)]
}

func span(d time.Duration) string {
	m := max(0, int(d.Minutes()))
	if m >= 60 {
		return fmt.Sprintf("%dh%02d", m/60, m%60)
	}
	return fmt.Sprintf("%dm", m)
}
