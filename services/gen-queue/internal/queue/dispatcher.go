package queue

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/ol1n/AiStack/gen-queue/internal/backend"
	"github.com/ol1n/AiStack/gen-queue/internal/store"
)

type jobEntry struct {
	job     *Job
	payload []byte
}

type Dispatcher struct {
	name    string
	mu      sync.Mutex
	cond    *sync.Cond
	pending []*jobEntry
	jobs    sync.Map // string -> *Job
	backend backend.Backend
	store   *store.ResultStore
	ttl     time.Duration
}

func New(name string, workers int, b backend.Backend, s *store.ResultStore, ttl time.Duration) *Dispatcher {
	d := &Dispatcher{
		name:    name,
		backend: b,
		store:   s,
		ttl:     ttl,
	}
	d.cond = sync.NewCond(&d.mu)
	for range workers {
		go d.workerLoop()
	}
	return d
}

func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // v4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func (d *Dispatcher) Submit(payload []byte) (*Job, int) {
	id := newID()
	job := newJob(id)
	d.jobs.Store(id, job)

	d.mu.Lock()
	d.pending = append(d.pending, &jobEntry{job: job, payload: payload})
	pos := len(d.pending) - 1
	d.mu.Unlock()

	d.cond.Signal()
	return job, pos
}

func (d *Dispatcher) Lookup(id string) (*Job, bool) {
	v, ok := d.jobs.Load(id)
	if !ok {
		return nil, false
	}
	return v.(*Job), true
}

// Position returns the 0-based index of job id in the pending slice,
// or -1 if the job is running or finished.
func (d *Dispatcher) Position(id string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	for i, e := range d.pending {
		if e.job.ID == id {
			return i
		}
	}
	return -1
}

func (d *Dispatcher) workerLoop() {
	for {
		d.mu.Lock()
		for len(d.pending) == 0 {
			d.cond.Wait()
		}
		e := d.pending[0]
		d.pending = d.pending[1:]
		d.mu.Unlock()

		d.process(e)
	}
}

var retryDelays = []time.Duration{0, 5 * time.Second, 10 * time.Second}

func (d *Dispatcher) process(e *jobEntry) {
	e.job.SetRunning()
	slog.Info("job started", "dispatcher", d.name, "id", e.job.ID)

	var result []byte
	var err error
	for attempt, delay := range retryDelays {
		if delay > 0 {
			time.Sleep(delay)
		}
		result, err = d.backend.Infer(context.Background(), e.payload)
		if err == nil {
			break
		}
		slog.Warn("job attempt failed",
			"dispatcher", d.name, "id", e.job.ID,
			"attempt", attempt+1, "err", err)
		if errors.Is(err, backend.ErrNonRetryable) {
			break
		}
	}

	if err != nil {
		slog.Error("job failed", "dispatcher", d.name, "id", e.job.ID, "err", err)
		e.job.SetError(err.Error())
		return
	}

	d.store.Put(e.job.ID, result, d.ttl)
	e.job.SetDone()
	slog.Info("job done", "dispatcher", d.name, "id", e.job.ID, "bytes", len(result))
}
