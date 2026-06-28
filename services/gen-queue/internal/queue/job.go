package queue

import (
	"sync"
	"time"
)

type Status string

const (
	StatusQueued  Status = "queued"
	StatusRunning Status = "running"
	StatusDone    Status = "done"
	StatusError   Status = "error"
)

type Job struct {
	ID        string
	CreatedAt time.Time

	mu     sync.Mutex
	status Status
	errMsg string
}

func newJob(id string) *Job {
	return &Job{
		ID:        id,
		CreatedAt: time.Now(),
		status:    StatusQueued,
	}
}

func (j *Job) SetRunning() {
	j.mu.Lock()
	j.status = StatusRunning
	j.mu.Unlock()
}

func (j *Job) SetDone() {
	j.mu.Lock()
	j.status = StatusDone
	j.mu.Unlock()
}

func (j *Job) SetError(msg string) {
	j.mu.Lock()
	j.status = StatusError
	j.errMsg = msg
	j.mu.Unlock()
}

func (j *Job) State() (Status, string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.status, j.errMsg
}
