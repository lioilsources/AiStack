package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/ol1n/AiStack/controller-manager/internal/compose"
	"github.com/ol1n/AiStack/controller-manager/internal/config"
	"github.com/ol1n/AiStack/controller-manager/internal/health"
	"github.com/ol1n/AiStack/controller-manager/internal/state"
)

type Handler struct {
	cfg    *config.Config
	store  *state.Store
	runner compose.Runner
	mu     sync.Mutex
}

func New(cfg *config.Config, store *state.Store, runner compose.Runner) *Handler {
	return &Handler{cfg: cfg, store: store, runner: runner}
}

func (h *Handler) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /activate", h.activate)
	mux.HandleFunc("GET /status", h.status)
	mux.HandleFunc("GET /health", h.liveness)
	mux.HandleFunc("POST /deactivate", h.deactivate)
}

func (h *Handler) activate(w http.ResponseWriter, r *http.Request) {
	alias := r.URL.Query().Get("model")
	if alias == "" {
		writeError(w, http.StatusBadRequest, "model parameter required")
		return
	}
	model, ok := h.cfg.Models[alias]
	if !ok {
		writeError(w, http.StatusBadRequest, "unknown model: "+alias)
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	st, err := h.store.Read()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "read state: "+err.Error())
		return
	}
	previous := st.Active

	// Stop current model if different.
	if previous != "" && previous != alias {
		prev, ok := h.cfg.Models[previous]
		if ok {
			slog.Info("stopping model", "model", previous)
			if err := h.runner.Down(r.Context(), prev.ComposeFile, prev.Alias, prev.Services); err != nil {
				slog.Warn("failed to stop previous model", "model", previous, "err", err)
			}
		}
	}

	// Start requested model.
	slog.Info("starting model", "model", alias)
	if err := h.runner.Up(r.Context(), model.ComposeFile, model.Alias, model.Services); err != nil {
		writeError(w, http.StatusInternalServerError, "start model: "+err.Error())
		if previous != "" && previous != alias {
			h.tryRestore(r.Context(), previous)
		}
		return
	}

	// Wait for health.
	timeout := model.HealthTimeoutDuration()
	slog.Info("waiting for health", "model", alias, "url", model.HealthURL, "timeout", timeout)
	if err := health.WaitHealthy(r.Context(), model.HealthURL, health.DefaultInterval, timeout); err != nil {
		slog.Error("health check failed", "model", alias, "err", err)
		writeError(w, http.StatusServiceUnavailable, "health check failed: "+err.Error())
		if previous != "" && previous != alias {
			h.tryRestore(r.Context(), previous)
		}
		return
	}

	now := time.Now()
	if err := h.store.Write(state.State{Active: alias, Healthy: true, Since: now}); err != nil {
		slog.Warn("persist state failed", "err", err)
	}
	slog.Info("model activated", "model", alias)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"active": alias, "since": now.Format(time.RFC3339)}) //nolint:errcheck
}

func (h *Handler) tryRestore(ctx context.Context, alias string) {
	model, ok := h.cfg.Models[alias]
	if !ok {
		return
	}
	slog.Info("restoring previous model", "model", alias)
	if err := h.runner.Up(ctx, model.ComposeFile, model.Alias, model.Services); err != nil {
		slog.Error("restore failed", "model", alias, "err", err)
		return
	}
	timeout := model.HealthTimeoutDuration()
	if err := health.WaitHealthy(ctx, model.HealthURL, health.DefaultInterval, timeout); err != nil {
		slog.Error("restore health failed", "model", alias, "err", err)
		_ = h.store.Write(state.State{Active: alias, Healthy: false, Since: time.Now()})
		return
	}
	_ = h.store.Write(state.State{Active: alias, Healthy: true, Since: time.Now()})
}

func (h *Handler) status(w http.ResponseWriter, r *http.Request) {
	st, err := h.store.Read()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(st) //nolint:errcheck
}

func (h *Handler) liveness(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

func (h *Handler) deactivate(w http.ResponseWriter, r *http.Request) {
	h.mu.Lock()
	defer h.mu.Unlock()

	for alias, model := range h.cfg.Models {
		slog.Info("stopping model", "model", alias)
		if err := h.runner.Down(r.Context(), model.ComposeFile, model.Alias, model.Services); err != nil {
			slog.Warn("stop failed", "model", alias, "err", err)
		}
	}
	_ = h.store.Write(state.State{Active: "", Healthy: false, Since: time.Now()})
	w.WriteHeader(http.StatusOK)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg}) //nolint:errcheck
}
