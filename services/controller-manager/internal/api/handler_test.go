package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/ol1n/AiStack/controller-manager/internal/config"
	"github.com/ol1n/AiStack/controller-manager/internal/state"
)

// mockRunner records calls without invoking docker.
type mockRunner struct {
	upCalls   []string
	downCalls []string
	upErr     error
}

func (m *mockRunner) Up(_ context.Context, composeFile string, _ []string) error {
	m.upCalls = append(m.upCalls, composeFile)
	return m.upErr
}

func (m *mockRunner) Down(_ context.Context, composeFile string, _ []string) error {
	m.downCalls = append(m.downCalls, composeFile)
	return nil
}

func newTestHandler(t *testing.T, runner *mockRunner) (*Handler, *httptest.Server) {
	t.Helper()

	// Minimal health server that always returns 200.
	healthSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(healthSrv.Close)

	cfg := &config.Config{
		Models: map[string]config.ModelConfig{
			"lab": {ComposeFile: "/opt/ai-stack/deploy/docker-compose.llm.yaml", Services: []string{"lab", "litellm"}, HealthURL: healthSrv.URL, Alias: "lab"},
			"ocr": {ComposeFile: "/opt/ai-stack/deploy/docker-compose.ocr.yaml", HealthURL: healthSrv.URL, Alias: "ocr"},
		},
	}
	store := state.New(filepath.Join(t.TempDir(), "state.json"))
	h := New(cfg, store, runner)
	return h, healthSrv
}

func TestActivate_success(t *testing.T) {
	runner := &mockRunner{}
	h, _ := newTestHandler(t, runner)

	mux := http.NewServeMux()
	h.Register(mux)

	req := httptest.NewRequest(http.MethodPost, "/activate?model=lab", nil)
	rr := httptest.NewRecorder()
	mux.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	if len(runner.upCalls) != 1 {
		t.Fatalf("expected 1 Up call, got %d", len(runner.upCalls))
	}
}

func TestActivate_missingModel(t *testing.T) {
	runner := &mockRunner{}
	h, _ := newTestHandler(t, runner)

	mux := http.NewServeMux()
	h.Register(mux)

	req := httptest.NewRequest(http.MethodPost, "/activate?model=nonexistent", nil)
	rr := httptest.NewRecorder()
	mux.ServeHTTP(rr, req)

	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rr.Code)
	}
}

func TestActivate_stopsPreviousModel(t *testing.T) {
	runner := &mockRunner{}
	h, _ := newTestHandler(t, runner)

	mux := http.NewServeMux()
	h.Register(mux)

	// Activate lab first.
	mux.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/activate?model=lab", nil))

	// Now switch to ocr.
	rr := httptest.NewRecorder()
	mux.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/activate?model=ocr", nil))

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	if len(runner.downCalls) == 0 {
		t.Fatal("expected Down to be called for previous model")
	}
}

func TestStatus(t *testing.T) {
	runner := &mockRunner{}
	h, _ := newTestHandler(t, runner)

	mux := http.NewServeMux()
	h.Register(mux)

	req := httptest.NewRequest(http.MethodGet, "/status", nil)
	rr := httptest.NewRecorder()
	mux.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
	var st state.State
	if err := json.NewDecoder(rr.Body).Decode(&st); err != nil {
		t.Fatal(err)
	}
}

func TestLiveness(t *testing.T) {
	runner := &mockRunner{}
	h, _ := newTestHandler(t, runner)

	mux := http.NewServeMux()
	h.Register(mux)

	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	rr := httptest.NewRecorder()
	mux.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
}

func TestDeactivate(t *testing.T) {
	runner := &mockRunner{}
	h, _ := newTestHandler(t, runner)

	mux := http.NewServeMux()
	h.Register(mux)

	req := httptest.NewRequest(http.MethodPost, "/deactivate", nil)
	rr := httptest.NewRecorder()
	mux.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
	if len(runner.downCalls) != 2 {
		t.Fatalf("expected 2 Down calls (all models), got %d", len(runner.downCalls))
	}
}
