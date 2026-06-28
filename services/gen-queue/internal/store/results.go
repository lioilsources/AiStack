package store

import (
	"sync"
	"time"
)

type entry struct {
	data []byte
}

// ResultStore holds completed PNG results with TTL-based eviction.
type ResultStore struct {
	mu      sync.RWMutex
	entries map[string]entry
}

func New() *ResultStore {
	return &ResultStore{entries: make(map[string]entry)}
}

func (s *ResultStore) Put(id string, data []byte, ttl time.Duration) {
	s.mu.Lock()
	s.entries[id] = entry{data: data}
	s.mu.Unlock()

	time.AfterFunc(ttl, func() {
		s.mu.Lock()
		delete(s.entries, id)
		s.mu.Unlock()
	})
}

func (s *ResultStore) Get(id string) ([]byte, bool) {
	s.mu.RLock()
	e, ok := s.entries[id]
	s.mu.RUnlock()
	return e.data, ok
}
