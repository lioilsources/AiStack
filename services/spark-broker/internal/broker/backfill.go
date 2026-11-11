package broker

import (
	"fmt"
	"strings"
	"time"
)

// Backfill (owner, 11. 11. 2026): "when a job wants to run only 35 minutes and in
// front of it sits an 8 h job with a higher priority, let the shorter one go first."
//
// Two rules, both only for jobs that fit the current profile (no switch):
//
//  1. Classic backfill. A higher job that cannot start yet (blocked by a running
//     job, a user window, its --after dependency, its --at time) holds a
//     reservation with its expected start. A lower job that would stand in its
//     way (non-preemptible, or taking a limited add-on slot) may start only if
//     its declared duration ends before that start. Unknown start = nothing that
//     would block it may start (same as before backfill existed).
//  2. Short first. A higher job that could switch the profile right now waits for
//     one short preemptible job on the current profile (queued or already
//     running) when the short job takes ≤ short_max_min and ≤ short_max_pct of the
//     long job's remaining duration, the long job's total accepted delay stays
//     ≤ max_delay_min and no queued deadline would be missed. Never for user
//     windows, recurring windows or due --at actions. After the promised end the
//     short job is drained like any other, so the long job cannot starve.

// reservation: a higher job that cannot start now and must not be delayed further.
type reservation struct {
	job    *Job
	at     *time.Time // expected start; nil = unknown
	target string     // profile it needs
}

// leaseMinutes: how long a lease stage holds the environment (see start).
func leaseMinutes(st *Stage) int {
	mins := st.LeaseMin
	if mins == 0 {
		mins = st.EstimateMin * 3 / 2
	}
	if mins < 15 {
		mins = 60
	}
	return mins
}

// minutesOf: declared duration of one stage, 0 = unknown/unbounded.
func minutesOf(j *Job, st *Stage) int {
	if st.Mode == ModeLease {
		if j.UntilIdle || j.Recurring != "" {
			return 0
		}
		return leaseMinutes(st)
	}
	return st.EstimateMin
}

// stageMinutes: declared duration of the current stage.
func stageMinutes(j *Job) int {
	if st := j.Stage(); st != nil {
		return minutesOf(j, st)
	}
	return 0
}

// remainingMinutes: declared duration of all stages still ahead (known ones only,
// so it is a lower bound).
func remainingMinutes(j *Job) int {
	n := 0
	for i := j.StageIdx; i < len(j.Stages); i++ {
		n += minutesOf(j, &j.Stages[i])
	}
	return n
}

// jobEnd: when an active job is expected to free its resources, nil = unknown.
func jobEnd(a *Job) *time.Time {
	if a.State == Draining {
		return a.DrainAt
	}
	st := a.Stage()
	if st == nil {
		return nil
	}
	if st.Mode == ModeLease {
		if a.UntilIdle {
			return nil
		}
		return a.LeaseUntil
	}
	if st.EstimateMin == 0 || a.StageStarted == nil {
		return nil
	}
	t := a.StageStarted.Add(time.Duration(st.EstimateMin) * time.Minute)
	return &t
}

// blocks: would job a, if it were running at time at, keep h from starting?
func (b *Broker) blocks(a, h *Job, target string, at time.Time) bool {
	if target != b.st.Profile {
		// h switches the profile: everything running must be drainable by h.
		return !a.Preemptible || (a.Priority >= h.Priority && !preemptsLong(h, a, at)) || preemptsLong(a, h, at)
	}
	// Same profile: only a limited add-on slot can be in the way.
	addons, _ := b.cat.Fits(target, h.Stage().Services)
	for _, ad := range addons {
		if b.cat.Addons[ad].MaxJobs > 0 && containsStr(a.Stage().Services, ad) {
			return true
		}
	}
	return false
}

// checkResv: may j start now without delaying any reservation? Returns the
// reservation j would block but fits before (to mark it as backfill), or the one
// it would delay (deny).
func (b *Broker) checkResv(j *Job, resv []reservation, now time.Time) (fitFor, deny *reservation) {
	for i := range resv {
		r := &resv[i]
		if r.job == j {
			continue
		}
		at := now
		if r.at != nil {
			at = *r.at
		}
		if !b.blocks(j, r.job, r.target, at) {
			continue
		}
		d := stageMinutes(j)
		if r.at == nil || d == 0 || now.Add(time.Duration(d)*time.Minute).After(*r.at) {
			return nil, r
		}
		if fitFor == nil || r.at.Before(*fitFor.at) {
			fitFor = r
		}
	}
	return fitFor, nil
}

func resvWaiting(r *reservation, now time.Time) string {
	if r.at == nil {
		return "čeká, až se uvolní SPARK pro úlohu s vyšší prioritou"
	}
	return fmt.Sprintf("nestihne skončit před %s (%s, priorita %d), ta začne kolem %s",
		r.job.ID, r.job.Project, r.job.Priority, clock(*r.at, now))
}

// mayGoFirst: rule 2 — may a short job of mins minutes delay long job h?
// ahead = queued jobs that would start later too (deadline check).
func (b *Broker) mayGoFirst(h *Job, target string, mins int, ahead []*Job, now time.Time) bool {
	cfg := b.cat.Backfill
	if cfg.Off || h.OnBehalfOfUser || h.Recurring != "" || h.due(now) {
		return false
	}
	if mins <= 0 || mins > cfg.ShortMaxMin {
		return false
	}
	total := remainingMinutes(h)
	if total == 0 || mins*100 > cfg.ShortMaxPct*total {
		return false
	}
	if h.BackfillDelayMin+mins > cfg.MaxDelayMin {
		return false
	}
	sw := b.cat.Profiles[target].SwitchMin
	if sw == 0 {
		sw = 10
	}
	for _, q := range ahead {
		if q.Deadline == nil || b.fitsCurrent(q) {
			continue
		}
		rem := remainingMinutes(q)
		if rem == 0 {
			return false // cannot prove the deadline holds
		}
		end := now.Add(time.Duration(mins+sw+rem) * time.Minute)
		if end.After(*q.Deadline) {
			return false
		}
	}
	return true
}

// promiseGrace: a lease ends on its own a tick after its LeaseUntil; draining it in
// that gap would only send it back to the queue.
const promiseGrace = time.Minute

// promised: an active job let ahead of h that is still within its promise.
func promised(a, h *Job, now time.Time) bool {
	return a.State == Running && a.BackfillFor == h.ID && a.BackfillUntil != nil &&
		!now.After(a.BackfillUntil.Add(promiseGrace))
}

// shortFirst picks one short job to go ahead of h (rule 2). A running one is granted
// at once; a queued one is returned as planned and granted when it starts.
// q is the whole queue, idx the position of h in it.
func (b *Broker) shortFirst(h *Job, target string, active []*Job, q []*Job, idx int,
	resv []reservation, now time.Time) (granted *Job, planned *Job, mins int) {
	for _, a := range active {
		if a.State != Running || !a.Preemptible || a.Priority > h.Priority || a.BackfillFor != "" {
			continue
		}
		end := jobEnd(a)
		if end == nil || !end.After(now) {
			continue
		}
		m := int((end.Sub(now) + time.Minute - 1) / time.Minute)
		if b.mayGoFirst(h, target, m, q, now) {
			return a, nil, m
		}
	}
	for k := idx + 1; k < len(q); k++ {
		s := q[k]
		if !s.Preemptible || s.Priority > h.Priority || !b.fitsCurrent(s) {
			continue
		}
		if s.NotBefore != nil && now.Before(*s.NotBefore) {
			continue
		}
		if b.pendingDep(s) != nil {
			continue
		}
		addons, _ := b.cat.Fits(b.st.Profile, s.Stage().Services)
		if b.addonsBusy(addons) != "" {
			continue
		}
		if _, deny := b.checkResv(s, resv, now); deny != nil {
			continue
		}
		m := stageMinutes(s)
		if b.mayGoFirst(h, target, m, q[:k], now) {
			return nil, s, m
		}
	}
	return nil, nil, 0
}

// letAhead records that s goes ahead of h for mins minutes.
func (b *Broker) letAhead(s, h *Job, mins int, charge bool, now time.Time) {
	t := now.Add(time.Duration(mins) * time.Minute)
	s.BackfillFor, s.BackfillUntil = h.ID, &t
	if charge {
		h.BackfillDelayMin += mins
		s.event(now, "jde napřed před %s (%s, priorita %d): krátká úloha, do %s", h.ID, h.Project, h.Priority, clock(t, now))
		h.event(now, "pouští napřed krátkou %s (%s, %d min), zdržení celkem %d min", s.ID, s.Project, mins, h.BackfillDelayMin)
		return
	}
	s.event(now, "backfill před %s (%s, priorita %d): skončí do %s, než ta může začít", h.ID, h.Project, h.Priority, clock(t, now))
}

func idsOf(js []*Job) string {
	ids := make([]string, len(js))
	for i, j := range js {
		ids[i] = j.ID
	}
	return strings.Join(ids, ", ")
}
