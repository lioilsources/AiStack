package broker

import (
	"strings"
	"testing"
	"time"
)

func leaseFor(project string, prio, mins int, svcs ...string) SubmitReq {
	r := lease(project, prio, svcs...)
	r.Stages[0].LeaseMin = mins
	return r
}

// step advances time in 5 min steps, keeping the given leases alive.
func (e *env) step(d time.Duration, ids ...string) {
	for d > 0 {
		s := 5 * time.Minute
		if d < s {
			s = d
		}
		e.now = e.now.Add(s)
		for _, id := range ids {
			_, _ = e.b.Heartbeat(id, nil)
		}
		e.b.Tick()
		d -= s
	}
}

// Owner's case: 35 min job behind an 8 h job with a higher priority goes first.
func TestShortJobGoesBeforeLongHigherJob(t *testing.T) {
	e := newEnv(t, true, "director")
	long := e.submit(leaseFor("storyteller", 30, 480, "gptoss"))
	short := e.submit(leaseFor("webtoon", 20, 35, "director"))
	e.tick()
	e.want(short.ID, Running)
	e.want(long.ID, Queued)
	if got := e.state(long.ID).Waiting; !strings.Contains(got, "napřed krátká úloha "+short.ID) {
		t.Fatalf("waiting = %q", got)
	}
	if e.ex.ran("up gptoss") != 0 {
		t.Fatal("nemělo se přepínat, dokud běží krátká úloha")
	}
	e.step(30*time.Minute, short.ID, long.ID)
	e.want(short.ID, Running) // within its promise: not drained
	e.step(10*time.Minute, short.ID)
	e.want(short.ID, Done) // lease ran out
	e.tick()
	e.want(long.ID, Running)
	if got := e.state(long.ID).BackfillDelayMin; got != 35 {
		t.Fatalf("zdržení %d, čekal 35", got)
	}
}

// Short-first needs short ≤ 60 min and ≤ 15 % of the long job.
func TestShortFirstLimits(t *testing.T) {
	for name, c := range map[string]struct{ long, short int }{
		"podíl":   {120, 35}, // 35 > 18 (15 % of 120)
		"strop":   {600, 61}, // over 60 min
		"neznámo": {480, 0},  // short without a known duration
	} {
		t.Run(name, func(t *testing.T) {
			e := newEnv(t, true, "director")
			long := e.submit(leaseFor("storyteller", 30, c.long, "gptoss"))
			sr := leaseFor("webtoon", 20, c.short, "director")
			if c.short == 0 {
				sr.UntilIdle = true
			}
			short := e.submit(sr)
			e.tick() // switch
			e.tick()
			e.want(long.ID, Running)
			e.want(short.ID, Queued)
		})
	}
}

// A deadline of the long job that would be missed blocks short-first.
func TestShortFirstRespectsDeadline(t *testing.T) {
	e := newEnv(t, true, "director")
	lr := leaseFor("storyteller", 30, 480, "gptoss")
	dl := e.now.Add(8*time.Hour + 20*time.Minute) // 35 + 10 (switch) + 480 > 500
	lr.Deadline = &dl
	long := e.submit(lr)
	short := e.submit(leaseFor("webtoon", 20, 35, "director"))
	e.tick()
	e.tick()
	e.want(long.ID, Running)
	e.want(short.ID, Queued)
}

// Total delay of one long job is capped (60 min): a second 35 min job waits.
func TestShortFirstDelayCap(t *testing.T) {
	e := newEnv(t, true, "director")
	long := e.submit(leaseFor("storyteller", 30, 480, "gptoss"))
	first := e.submit(leaseFor("webtoon", 20, 35, "director"))
	e.tick()
	e.want(first.ID, Running)
	second := e.submit(leaseFor("webtoon", 20, 90, "director"))
	e.tick()
	e.want(second.ID, Running) // preemptible lower job may run meanwhile (as before)…
	e.step(40*time.Minute, first.ID, second.ID)
	e.want(first.ID, Done)
	e.want(second.ID, Draining) // …but it is not let ahead: 35 + 50 left > 60
	e.adv(6 * time.Minute)
	e.tick()
	e.want(long.ID, Running)
}

// An already running short lease is not drained by the long job.
func TestRunningShortJobIsNotDrained(t *testing.T) {
	e := newEnv(t, true, "director")
	short := e.submit(leaseFor("webtoon", 20, 30, "director"))
	e.tick()
	e.want(short.ID, Running)
	e.step(10*time.Minute, short.ID)
	long := e.submit(leaseFor("storyteller", 30, 480, "gptoss"))
	e.tick()
	e.want(short.ID, Running)
	if j := e.state(short.ID); j.BackfillFor != long.ID {
		t.Fatalf("backfill_for = %q", j.BackfillFor)
	}
	e.step(25*time.Minute, short.ID)
	e.want(short.ID, Done)
	e.tick()
	e.want(long.ID, Running)
	if got := e.state(long.ID).BackfillDelayMin; got != 20 {
		t.Fatalf("zdržení %d, čekal 20", got)
	}
}

// A short run job that overruns its estimate is drained after the promise: no starvation.
func TestShortRunOverrunIsDrained(t *testing.T) {
	e := newEnv(t, true, "director")
	long := e.submit(leaseFor("storyteller", 30, 480, "gptoss"))
	short := e.submit(SubmitReq{Project: "library", Priority: 10, Resume: true,
		Stages: []Stage{{Services: []string{"director"}, Mode: ModeRun, Command: "enrich", EstimateMin: 30}}})
	e.tick()
	e.want(short.ID, Running)
	e.step(25*time.Minute, long.ID)
	e.want(short.ID, Running)
	e.step(10*time.Minute, long.ID)
	e.want(short.ID, Draining)
}

// User windows never wait for short jobs.
func TestUserWindowDoesNotWaitForShortJob(t *testing.T) {
	e := newEnv(t, true, "director")
	w := e.submit(SubmitReq{Project: "user", OnBehalfOfUser: true, UserQuote: "chci comfy na 3 hodiny",
		Stages: []Stage{{Services: []string{"comfyui"}, LeaseMin: 180}}})
	short := e.submit(leaseFor("webtoon", 20, 30, "director"))
	e.tick()
	e.tick()
	e.want(w.ID, Running)
	e.want(short.ID, Queued)
}

// Classic backfill: while a user window blocks the head, a non-preemptible lower
// lease may start only if it ends before the window does.
func TestClassicBackfillBeforeBlockedHead(t *testing.T) {
	e := newEnv(t, true, "comfy")
	w := e.submit(SubmitReq{Project: "user", OnBehalfOfUser: true, UserQuote: "chci comfy na 3 hodiny",
		Stages: []Stage{{Services: []string{"comfyui"}, LeaseMin: 180}}})
	e.tick()
	e.want(w.ID, Running)
	head := e.submit(leaseFor("storyteller", 40, 120, "director"))
	fits := leaseFor("webtoon", 20, 60, "comfyui")
	fits.Preemptible = new(bool)
	tooLong := leaseFor("webtoon", 20, 240, "comfyui")
	tooLong.Preemptible = new(bool)
	f := e.submit(fits)
	tl := e.submit(tooLong)
	e.tick()
	e.want(head.ID, Queued)
	e.want(f.ID, Running)
	if j := e.state(f.ID); j.BackfillFor != head.ID {
		t.Fatalf("backfill_for = %q", j.BackfillFor)
	}
	e.want(tl.ID, Queued)
	if got := e.state(tl.ID).Waiting; !strings.Contains(got, "nestihne skončit před "+head.ID) {
		t.Fatalf("waiting = %q", got)
	}
}

// Classic backfill around an --after dependency with a known end.
func TestClassicBackfillBeforeDependency(t *testing.T) {
	e := newEnv(t, true, "llm")
	first := e.submit(leaseFor("webtoon", 30, 60, "qwen36"))
	e.tick()
	e.want(first.ID, Running)
	next := e.submit(SubmitReq{Project: "webtoon", Priority: 30, After: []string{first.ID},
		Stages: []Stage{{Services: []string{"comfyui"}, Mode: ModeLease, LeaseMin: 60}}})
	long := leaseFor("library", 10, 120, "qwen36")
	long.Preemptible = new(bool)
	short := leaseFor("library", 10, 30, "qwen36")
	short.Preemptible = new(bool)
	lj := e.submit(long)
	sj := e.submit(short)
	e.tick()
	e.want(next.ID, Queued)
	e.want(lj.ID, Queued)
	e.want(sj.ID, Running)
}

func longRun(project string, prio, est int, profile string) SubmitReq {
	return SubmitReq{Project: project, Title: project + " dávka", Priority: prio, Resume: true,
		Stages: []Stage{{Services: []string{profile}, Mode: ModeRun, Command: "batch " + project, EstimateMin: est}}}
}

// exitDrained ends the drained process like SIGTERM would and waits until it is queued.
func (e *env) exitDrained(id string, n int) {
	e.t.Helper()
	e.ex.procs[n].ch <- 143
	waitFor(e.t, func() bool { return e.state(id).State == Queued })
}

// Owner's case 3: a running resumable 8 h job is drained for a 35 min job on another
// profile, waits for it and then continues.
func TestShortJobPreemptsRunningLongJob(t *testing.T) {
	e := newEnv(t, true, "gptoss")
	long := e.submit(longRun("storyteller", 30, 480, "gptoss"))
	e.tick()
	e.want(long.ID, Running)
	e.step(60 * time.Minute)
	short := e.submit(leaseFor("webtoon", 20, 35, "director"))
	e.tick()
	e.want(long.ID, Draining)
	if j := e.state(long.ID); j.YieldFor != short.ID {
		t.Fatalf("yield_for = %q", j.YieldFor)
	}
	e.exitDrained(long.ID, 0)
	e.tick() // switch to director
	e.tick()
	e.want(short.ID, Running)
	e.want(long.ID, Queued)
	if got := e.state(long.ID).Waiting; !strings.Contains(got, "pustila napřed krátkou "+short.ID) {
		t.Fatalf("waiting = %q", got)
	}
	e.step(30*time.Minute, short.ID)
	e.want(short.ID, Running) // the long job does not drain it back
	e.step(10*time.Minute, short.ID)
	e.want(short.ID, Done)
	e.tick()
	e.tick()
	e.want(long.ID, Running)
	if got := e.state(long.ID).BackfillDelayMin; got != 55 { // 35 + 2 × 10 switch
		t.Fatalf("zdržení %d, čekal 55", got)
	}
}

// An endless filler (no estimate) yields to any short job, without the share/delay cap.
func TestEndlessFillerYields(t *testing.T) {
	e := newEnv(t, true, "gptoss")
	filler := e.submit(longRun("library", 30, 0, "gptoss"))
	e.tick()
	e.want(filler.ID, Running)
	short := e.submit(leaseFor("webtoon", 20, 60, "director"))
	e.tick()
	e.want(filler.ID, Draining)
	e.exitDrained(filler.ID, 0)
	e.tick()
	e.tick()
	e.want(short.ID, Running)
}

// Not for a long job without resume, not for a short job that is too long or unbounded.
func TestPreemptLimits(t *testing.T) {
	for name, c := range map[string]struct {
		resume bool
		short  int
	}{
		"bez resume": {false, 35},
		"strop":      {true, 61},
		"neznámo":    {true, 0},
	} {
		t.Run(name, func(t *testing.T) {
			e := newEnv(t, true, "gptoss")
			lr := longRun("storyteller", 30, 480, "gptoss")
			lr.Resume = c.resume
			if !c.resume {
				lr.Preemptible = new(bool)
			}
			long := e.submit(lr)
			e.tick()
			sr := leaseFor("webtoon", 20, c.short, "director")
			if c.short == 0 {
				sr.UntilIdle = true
			}
			short := e.submit(sr)
			e.tick()
			e.want(long.ID, Running)
			e.want(short.ID, Queued)
		})
	}
}

// A short job that overruns its promise is drained by the job it went ahead of.
func TestPreemptingShortOverrunIsDrained(t *testing.T) {
	e := newEnv(t, true, "gptoss")
	long := e.submit(longRun("storyteller", 30, 480, "gptoss"))
	e.tick()
	short := e.submit(SubmitReq{Project: "library", Priority: 10, Resume: true,
		Stages: []Stage{{Services: []string{"director"}, Mode: ModeRun, Command: "enrich", EstimateMin: 30}}})
	e.tick()
	e.exitDrained(long.ID, 0)
	e.tick()
	e.tick()
	e.want(short.ID, Running)
	e.step(25 * time.Minute)
	e.want(short.ID, Running)
	e.step(10 * time.Minute)
	e.want(short.ID, Draining)
}
