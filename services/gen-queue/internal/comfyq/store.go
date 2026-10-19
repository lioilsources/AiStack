// Package comfyq is the public ComfyUI job queue behind llm.ol1n.com/v1/image/*
// (PLAN-image-api.md). One global worker, so "max 1 ComfyUI job" holds across
// all projects; jobs wait for the comfy window instead of failing.
package comfyq

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

type Status string

const (
	StatusQueued    Status = "queued"
	StatusRunning   Status = "running"
	StatusDone      Status = "done"
	StatusError     Status = "error"
	StatusCancelled Status = "cancelled"
	// Reported only: a queued job while the gate is closed.
	StatusWaitingWindow Status = "waiting_window"
)

type Job struct {
	ID         string    `json:"id"`
	Project    string    `json:"project"`
	ClientRef  string    `json:"client_ref,omitempty"`
	Status     Status    `json:"status"`
	Error      string    `json:"error,omitempty"`
	Attempts   int       `json:"attempts"`
	NotBefore  time.Time `json:"not_before"`
	CreatedAt  time.Time `json:"created_at"`
	StartedAt  time.Time `json:"started_at"`
	FinishedAt time.Time `json:"finished_at"`
	PromptID   string    `json:"prompt_id,omitempty"`
	Inputs     []string  `json:"inputs,omitempty"`
	// Paths relative to the data dir; nothing here is ever deleted.
	Outputs []string `json:"outputs,omitempty"`
}

func (j *Job) terminal() bool {
	return j.Status == StatusDone || j.Status == StatusError || j.Status == StatusCancelled
}

// Store keeps every job as one JSON file under <dir>/jobs, plus its workflow
// and input images in <dir>/jobs/<id>/. Restart-safe and no CGO (the image is
// built with CGO_ENABLED=0, so no SQLite).
type Store struct {
	dir  string
	mu   sync.Mutex
	jobs map[string]*Job
}

func OpenStore(dir string) (*Store, error) {
	s := &Store{dir: dir, jobs: map[string]*Job{}}
	if err := os.MkdirAll(filepath.Join(dir, "jobs"), 0o755); err != nil {
		return nil, err
	}
	files, err := filepath.Glob(filepath.Join(dir, "jobs", "*.json"))
	if err != nil {
		return nil, err
	}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		var j Job
		if err := json.Unmarshal(raw, &j); err != nil {
			return nil, fmt.Errorf("%s: %w", f, err)
		}
		// A job that was running when the process died goes back to the queue.
		if j.Status == StatusRunning {
			j.Status = StatusQueued
			j.PromptID = ""
		}
		s.jobs[j.ID] = &j
	}
	return s, nil
}

func (s *Store) jobDir(id string) string { return filepath.Join(s.dir, "jobs", id) }

// save writes j atomically. Caller holds s.mu.
func (s *Store) save(j *Job) error {
	raw, err := json.MarshalIndent(j, "", "  ")
	if err != nil {
		return err
	}
	path := filepath.Join(s.dir, "jobs", j.ID+".json")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Update applies fn to job id under the lock and persists it.
func (s *Store) Update(id string, fn func(*Job)) (Job, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok {
		return Job{}, fmt.Errorf("job %s not found", id)
	}
	fn(j)
	return *j, s.save(j)
}

func (s *Store) Get(id string) (Job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok {
		return Job{}, false
	}
	return *j, true
}

// Add stores a new job with its workflow and inputs. If the project already
// has a job with the same client_ref, that job is returned and existed=true.
func (s *Store) Add(j *Job, workflow []byte, inputs map[string][]byte) (Job, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if j.ClientRef != "" {
		for _, o := range s.jobs {
			if o.Project == j.Project && o.ClientRef == j.ClientRef {
				return *o, true, nil
			}
		}
	}
	dir := s.jobDir(j.ID)
	if err := os.MkdirAll(filepath.Join(dir, "in"), 0o755); err != nil {
		return Job{}, false, err
	}
	if err := os.WriteFile(filepath.Join(dir, "workflow.json"), workflow, 0o644); err != nil {
		return Job{}, false, err
	}
	for name, data := range inputs {
		if err := os.WriteFile(filepath.Join(dir, "in", name), data, 0o644); err != nil {
			return Job{}, false, err
		}
		j.Inputs = append(j.Inputs, name)
	}
	sort.Strings(j.Inputs)
	s.jobs[j.ID] = j
	return *j, false, s.save(j)
}

func (s *Store) Workflow(id string) ([]byte, error) {
	return os.ReadFile(filepath.Join(s.jobDir(id), "workflow.json"))
}

func (s *Store) Input(id, name string) ([]byte, error) {
	return os.ReadFile(filepath.Join(s.jobDir(id), "in", name))
}

// ordered returns queued jobs in dispatch order: project rank, then age.
func (s *Store) ordered(rank func(string) int) []Job {
	s.mu.Lock()
	var q []Job
	for _, j := range s.jobs {
		if j.Status == StatusQueued {
			q = append(q, *j)
		}
	}
	s.mu.Unlock()
	sort.Slice(q, func(a, b int) bool {
		ra, rb := rank(q[a].Project), rank(q[b].Project)
		if ra != rb {
			return ra < rb
		}
		return q[a].CreatedAt.Before(q[b].CreatedAt)
	})
	return q
}

// Counts returns how many jobs are in each non-terminal status, per project.
func (s *Store) Counts() map[string]map[Status]int {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string]map[Status]int{}
	for _, j := range s.jobs {
		if j.terminal() {
			continue
		}
		if out[j.Project] == nil {
			out[j.Project] = map[Status]int{}
		}
		out[j.Project][j.Status]++
	}
	return out
}

// OutputPath is where output n of a job is written, relative to the data dir.
func OutputPath(j Job, n int, ext string) string {
	return filepath.Join("out", j.Project, j.CreatedAt.Format("2006-01-02"), fmt.Sprintf("%s_%d%s", j.ID, n, ext))
}

func (s *Store) Abs(rel string) string { return filepath.Join(s.dir, rel) }
