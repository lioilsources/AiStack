package comfyq

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

type Config struct {
	// Projects in priority order (PLAN-spark-scheduler §1); unknown ones last.
	Priority    []string
	JobTimeout  time.Duration
	MaxAttempts int
	// Backoff before attempt n+1 (last value repeats).
	Backoff []time.Duration
	// How long ComfyUI may stay unreachable during a run before the job is
	// requeued (window switch, crash).
	LostAfter time.Duration
	PollEvery time.Duration
	IdleEvery time.Duration
}

type Queue struct {
	cfg    Config
	store  *Store
	gate   *Gate
	comfy  *Client
	wake   chan struct{}
	mu     sync.Mutex
	runID  string
	cancel context.CancelFunc
}

func NewQueue(cfg Config, s *Store, g *Gate, c *Client) *Queue {
	return &Queue{cfg: cfg, store: s, gate: g, comfy: c, wake: make(chan struct{}, 1)}
}

func (q *Queue) rank(project string) int {
	for i, p := range q.cfg.Priority {
		if p == project {
			return i
		}
	}
	return len(q.cfg.Priority)
}

func (q *Queue) poke() {
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

// Position is the 0-based place of a queued job in dispatch order, or -1.
func (q *Queue) Position(id string) int {
	for i, j := range q.store.ordered(q.rank) {
		if j.ID == id {
			return i
		}
	}
	return -1
}

func (q *Queue) next(now time.Time) (Job, bool) {
	for _, j := range q.store.ordered(q.rank) {
		if !j.NotBefore.After(now) {
			return j, true
		}
	}
	return Job{}, false
}

// Run is the single global worker.
func (q *Queue) Run(ctx context.Context) {
	for {
		wait := q.cfg.IdleEvery
		if j, ok := q.next(time.Now()); ok {
			st := q.gate.Check(ctx)
			if st.Open() {
				q.process(ctx, j)
				q.gate.Invalidate()
				continue
			}
			slog.Debug("image job waits", "id", j.ID, "gate", st.State, "reason", st.Reason)
		}
		select {
		case <-ctx.Done():
			return
		case <-q.wake:
		case <-time.After(wait):
		}
	}
}

// Cancel cancels a job of project. A queued job is just marked; a running one
// is interrupted in ComfyUI only if ComfyUI is executing that very prompt.
func (q *Queue) Cancel(ctx context.Context, id, project string) (Job, error) {
	j, ok := q.store.Get(id)
	if !ok {
		return Job{}, errNotFound
	}
	if j.Project != project {
		return Job{}, errForbidden
	}
	if j.terminal() {
		return j, nil
	}
	// Mark first, so the worker sees "cancelled" whatever the run returns.
	j, err := q.store.Update(id, func(j *Job) {
		if !j.terminal() {
			j.Status, j.FinishedAt = StatusCancelled, time.Now()
		}
	})
	if err != nil {
		return j, err
	}
	q.mu.Lock()
	running := q.runID == id
	cancel := q.cancel
	q.mu.Unlock()
	if running && cancel != nil {
		if pid := j.PromptID; pid != "" {
			if cur, err := q.comfy.RunningPrompt(ctx); err == nil && cur == pid {
				_ = q.comfy.Interrupt(ctx)
			}
		}
		cancel()
	}
	return j, nil
}

var (
	errNotFound  = errors.New("job not found")
	errForbidden = errors.New("job belongs to another project")
)

func (q *Queue) process(parent context.Context, j Job) {
	ctx, cancel := context.WithTimeout(parent, q.cfg.JobTimeout)
	defer cancel()
	q.mu.Lock()
	q.runID, q.cancel = j.ID, cancel
	q.mu.Unlock()
	defer func() {
		q.mu.Lock()
		q.runID, q.cancel = "", nil
		q.mu.Unlock()
	}()

	j, err := q.store.Update(j.ID, func(j *Job) {
		j.Status, j.StartedAt, j.Attempts, j.Error = StatusRunning, time.Now(), j.Attempts+1, ""
	})
	if err != nil {
		slog.Error("image job update", "id", j.ID, "err", err)
		return
	}
	slog.Info("image job started", "id", j.ID, "project", j.Project, "attempt", j.Attempts)

	outputs, err := q.run(ctx, j)
	// finish applies fn unless the job was cancelled meanwhile.
	finish := func(fn func(*Job)) {
		q.store.Update(j.ID, func(j *Job) { //nolint:errcheck
			if j.Status != StatusCancelled {
				fn(j)
			}
		})
	}
	if cur, _ := q.store.Get(j.ID); cur.Status == StatusCancelled {
		slog.Info("image job cancelled", "id", j.ID)
		return
	}
	switch {
	case err == nil:
		finish(func(j *Job) { j.Status, j.Outputs, j.FinishedAt = StatusDone, outputs, time.Now() })
		slog.Info("image job done", "id", j.ID, "project", j.Project, "outputs", len(outputs),
			"seconds", time.Since(j.StartedAt).Round(time.Second).Seconds())
	case errors.Is(err, ErrInvalid), errors.Is(err, context.DeadlineExceeded), j.Attempts >= q.cfg.MaxAttempts:
		finish(func(j *Job) { j.Status, j.Error, j.FinishedAt = StatusError, err.Error(), time.Now() })
		slog.Error("image job failed", "id", j.ID, "project", j.Project, "attempt", j.Attempts, "err", err)
	default:
		delay := q.cfg.Backoff[min(j.Attempts-1, len(q.cfg.Backoff)-1)]
		finish(func(j *Job) {
			j.Status, j.Error, j.PromptID, j.NotBefore = StatusQueued, err.Error(), "", time.Now().Add(delay)
		})
		slog.Warn("image job requeued", "id", j.ID, "project", j.Project, "attempt", j.Attempts, "in", delay, "err", err)
	}
}

func (q *Queue) run(ctx context.Context, j Job) ([]string, error) {
	for _, name := range j.Inputs {
		data, err := q.store.Input(j.ID, name)
		if err != nil {
			return nil, fmt.Errorf("%w: input %s: %v", ErrInvalid, name, err)
		}
		if err := q.comfy.Upload(ctx, name, data); err != nil {
			return nil, err
		}
	}
	wf, err := q.store.Workflow(j.ID)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	pid, err := q.comfy.Submit(ctx, json.RawMessage(wf), "image-api-"+j.ID)
	if err != nil {
		return nil, err
	}
	q.store.Update(j.ID, func(j *Job) { j.PromptID = pid }) //nolint:errcheck

	var lostSince time.Time
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(q.cfg.PollEvery):
		}
		done, files, err := q.comfy.Poll(ctx, pid)
		if errors.Is(err, ErrInvalid) || errors.Is(err, ErrInterrupted) {
			return nil, err
		}
		if err != nil {
			if lostSince.IsZero() {
				lostSince = time.Now()
			}
			if time.Since(lostSince) > q.cfg.LostAfter {
				return nil, fmt.Errorf("ComfyUI lost during run: %v", err)
			}
			continue
		}
		lostSince = time.Time{}
		if done {
			return q.save(ctx, j, files)
		}
	}
}

func (q *Queue) save(ctx context.Context, j Job, files []OutputFile) ([]string, error) {
	if len(files) == 0 {
		return nil, fmt.Errorf("%w: workflow has no saved outputs (SaveImage, not PreviewImage)", ErrInvalid)
	}
	var out []string
	for n, f := range files {
		data, err := q.comfy.View(ctx, f)
		if err != nil {
			return nil, err
		}
		ext := strings.ToLower(filepath.Ext(f.Filename))
		rel := OutputPath(j, n, ext)
		abs := q.store.Abs(rel)
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			return nil, err
		}
		if err := os.WriteFile(abs, data, 0o644); err != nil {
			return nil, err
		}
		out = append(out, rel)
	}
	return out, nil
}

func sortStrings(s []string) { sort.Strings(s) }

// sortNodeIDs orders ComfyUI node ids numerically when they are numbers.
func sortNodeIDs(ids []string) {
	sort.Slice(ids, func(a, b int) bool {
		if len(ids[a]) != len(ids[b]) {
			return len(ids[a]) < len(ids[b])
		}
		return ids[a] < ids[b]
	})
}
