package health

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestWaitHealthy_success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx := context.Background()
	if err := WaitHealthy(ctx, srv.URL, 10*time.Millisecond, 2*time.Second); err != nil {
		t.Fatal(err)
	}
}

func TestWaitHealthy_retries(t *testing.T) {
	var count atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if count.Add(1) < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx := context.Background()
	if err := WaitHealthy(ctx, srv.URL, 10*time.Millisecond, 2*time.Second); err != nil {
		t.Fatal(err)
	}
	if count.Load() < 3 {
		t.Fatalf("expected at least 3 calls, got %d", count.Load())
	}
}

func TestWaitHealthy_timeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	ctx := context.Background()
	err := WaitHealthy(ctx, srv.URL, 10*time.Millisecond, 50*time.Millisecond)
	if err == nil {
		t.Fatal("expected timeout error")
	}
}
