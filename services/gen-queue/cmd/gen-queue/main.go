package main

import (
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/ol1n/AiStack/gen-queue/internal/api"
	"github.com/ol1n/AiStack/gen-queue/internal/backend"
	"github.com/ol1n/AiStack/gen-queue/internal/queue"
	"github.com/ol1n/AiStack/gen-queue/internal/store"
)

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	nimKontextURL  := envOr("NIM_KONTEXT_URL", "http://host.docker.internal:8009/v1/infer")
	nimSchnellURL  := envOr("NIM_SCHNELL_URL", "http://host.docker.internal:8010/v1/infer")
	kontextWorkers := envInt("NIM_KONTEXT_WORKERS", 1)
	schnellWorkers := envInt("NIM_SCHNELL_WORKERS", 1)
	ttlSec         := envInt("RESULT_TTL_SECONDS", 3600)
	// Per-call NIM inference timeout. FLUX Kontext on GB10 routinely takes
	// 110–185 s; 180 s was too tight and caused spurious timeouts + retries.
	inferSec       := envInt("NIM_INFER_TIMEOUT_SECONDS", 300)
	addr           := envOr("LISTEN_ADDR", ":8091")
	// Inject "disable_safety_checker":true into NIM requests to bypass the
	// Cosmos prompt blocklist + output NSFW filter (self-hosted, own use).
	// Requires NIM_ALLOW_UNCHECKED_GENERATION=true on the NIM containers.
	disableSafety  := envOr("NIM_DISABLE_SAFETY_CHECKER", "true") == "true"

	ttl          := time.Duration(ttlSec) * time.Second
	inferTimeout := time.Duration(inferSec) * time.Second

	results := store.New()

	kontextBackend := backend.NewNimKontext(nimKontextURL, inferTimeout, disableSafety)
	schnellBackend := backend.NewNimSchnell(nimSchnellURL, inferTimeout, disableSafety)

	kontextDisp := queue.New("kontext", kontextWorkers, kontextBackend, results, ttl)
	schnellDisp := queue.New("schnell", schnellWorkers, schnellBackend, results, ttl)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok","service":"gen-queue"}`)) //nolint:errcheck
	})

	h := api.New(kontextDisp, schnellDisp, results)
	h.Register(mux)

	slog.Info("gen-queue ready",
		"addr", addr,
		"nim_kontext", nimKontextURL,
		"nim_schnell", nimSchnellURL,
		"kontext_workers", kontextWorkers,
		"schnell_workers", schnellWorkers,
		"result_ttl", ttl,
		"infer_timeout", inferTimeout,
		"disable_safety_checker", disableSafety,
	)

	if err := http.ListenAndServe(addr, mux); err != nil {
		slog.Error("server error", "err", err)
		os.Exit(1)
	}
}
