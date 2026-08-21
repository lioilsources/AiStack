package api

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"

	"github.com/ol1n/AiStack/gen-queue/internal/queue"
	"github.com/ol1n/AiStack/gen-queue/internal/store"
	"github.com/ol1n/AiStack/gen-queue/internal/ugc"
)

type Handler struct {
	kontext *queue.Dispatcher
	schnell *queue.Dispatcher
	store   *store.ResultStore
}

func New(kontext, schnell *queue.Dispatcher, s *store.ResultStore) *Handler {
	return &Handler{kontext: kontext, schnell: schnell, store: s}
}

func (h *Handler) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /nim/flux-kontext/v1/infer", h.submitHandler(h.kontext))
	mux.HandleFunc("GET /nim/flux-kontext/jobs/{id}", h.statusHandler(h.kontext))
	mux.HandleFunc("GET /nim/flux-kontext/jobs/{id}/result", h.resultHandler)

	mux.HandleFunc("POST /nim/flux-schnell/v1/infer", h.submitHandler(h.schnell))
	mux.HandleFunc("GET /nim/flux-schnell/jobs/{id}", h.statusHandler(h.schnell))
	mux.HandleFunc("GET /nim/flux-schnell/jobs/{id}/result", h.resultHandler)
}

func (h *Handler) submitHandler(d *queue.Dispatcher) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		payload, err := io.ReadAll(r.Body)
		if err != nil {
			jsonError(w, "failed to read request body", http.StatusBadRequest)
			return
		}
		if len(payload) == 0 {
			jsonError(w, "empty request body", http.StatusBadRequest)
			return
		}
		job, pos := d.Submit(payload)
		slog.Info("job submitted", "id", job.ID, "queue_position", pos)

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
			"id":             job.ID,
			"queue_position": pos,
		})
	}
}

func (h *Handler) statusHandler(d *queue.Dispatcher) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		job, ok := d.Lookup(id)
		if !ok {
			jsonError(w, "job not found", http.StatusNotFound)
			return
		}
		status, errMsg := job.State()
		resp := map[string]any{"status": string(status)}
		switch status {
		case queue.StatusQueued:
			resp["queue_position"] = d.Position(id)
		case queue.StatusError:
			resp["error"] = errMsg
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp) //nolint:errcheck
	}
}

func (h *Handler) resultHandler(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	data, ok := h.store.Get(id)
	if !ok {
		jsonError(w, "result not found", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Length", fmt.Sprintf("%d", len(data)))
	w.Write(data) //nolint:errcheck
}

func jsonError(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg}) //nolint:errcheck
}

// RegisterUGC exposes the UGC factory pipeline (see internal/ugc).
func RegisterUGC(mux *http.ServeMux, pipe *ugc.Pipeline) {
	mux.HandleFunc("POST /ugc/generate", func(w http.ResponseWriter, r *http.Request) {
		var req ugc.Request
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			jsonError(w, err.Error(), http.StatusBadRequest)
			return
		}
		job, err := pipe.Submit(req)
		if err != nil {
			jsonError(w, err.Error(), http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		json.NewEncoder(w).Encode(job) //nolint:errcheck
	})
	mux.HandleFunc("GET /ugc/jobs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(pipe.List()) //nolint:errcheck
	})
	mux.HandleFunc("GET /ugc/jobs/{id}", func(w http.ResponseWriter, r *http.Request) {
		job := pipe.Get(r.PathValue("id"))
		if job == nil {
			jsonError(w, "job not found", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(job) //nolint:errcheck
	})
}
