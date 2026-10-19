package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
	_ "time/tzdata" // alpine image has no zoneinfo; the comfy window is Prague time

	"github.com/ol1n/AiStack/gen-queue/internal/api"
	"github.com/ol1n/AiStack/gen-queue/internal/backend"
	"github.com/ol1n/AiStack/gen-queue/internal/comfyq"
	"github.com/ol1n/AiStack/gen-queue/internal/queue"
	"github.com/ol1n/AiStack/gen-queue/internal/store"
	"github.com/ol1n/AiStack/gen-queue/internal/ugc"
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

	nimKontextURL := envOr("NIM_KONTEXT_URL", "http://host.docker.internal:8009/v1/infer")
	nimSchnellURL := envOr("NIM_SCHNELL_URL", "http://host.docker.internal:8010/v1/infer")
	kontextWorkers := envInt("NIM_KONTEXT_WORKERS", 1)
	schnellWorkers := envInt("NIM_SCHNELL_WORKERS", 1)
	ttlSec := envInt("RESULT_TTL_SECONDS", 3600)
	// Per-call NIM inference timeout. FLUX Kontext on GB10 routinely takes
	// 110–185 s; 180 s was too tight and caused spurious timeouts + retries.
	inferSec := envInt("NIM_INFER_TIMEOUT_SECONDS", 300)
	addr := envOr("LISTEN_ADDR", ":8091")
	// Inject "disable_safety_checker":true into NIM requests to bypass the
	// Cosmos prompt blocklist + output NSFW filter (self-hosted, own use).
	// Requires NIM_ALLOW_UNCHECKED_GENERATION=true on the NIM containers.
	disableSafety := envOr("NIM_DISABLE_SAFETY_CHECKER", "true") == "true"

	ttl := time.Duration(ttlSec) * time.Second
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

	// UGC tovarna: koncept -> cleanplate -> TRELLIS mesh -> push na NAS.
	// Jeden serializovany worker (img->3D soubezne s dalsi generaci = znamy
	// power-spike crash GB10).
	ugcPipe := ugc.New(ugc.Config{
		Comfy:      ugc.NewComfy(envOr("UGC_COMFY_URL", "http://host.docker.internal:8188")),
		NAS:        ugc.NewNAS(envOr("UGC_NAS_URL", "http://192.168.88.88:8095"), envOr("UGC_SPOOL", "/spool")),
		Checkpoint: envOr("UGC_CHECKPOINT", "Illustrious-XL-v2.0.safetensors"),
		Timeout:    time.Duration(envInt("UGC_STAGE_TIMEOUT_SECONDS", 900)) * time.Second,
	})
	api.RegisterUGC(mux, ugcPipe)

	// Veřejné image API (PLAN-image-api.md): /v1/image/* na llm.ol1n.com,
	// jeden globální worker pro ComfyUI, joby čekají na okno comfy.
	if err := registerImageAPI(mux); err != nil {
		slog.Error("image api", "err", err)
		os.Exit(1)
	}

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

func registerImageAPI(mux *http.ServeMux) error {
	loc, err := time.LoadLocation(envOr("IMAGE_TZ", "Europe/Prague"))
	if err != nil {
		return err
	}
	// Stejný řetězec jako SCHEDULE v drop-inu rag-schedule; jen pro uzávěrku
	// před přepnutím a údaj o dalším okně. Zdroj pravdy je běžící ComfyUI.
	sched, err := comfyq.ParseSchedule(envOr("IMAGE_SCHEDULE", "7:comfy 13:llm"))
	if err != nil {
		return err
	}
	store, err := comfyq.OpenStore(envOr("IMAGE_DATA", "/data"))
	if err != nil {
		return err
	}
	client := comfyq.NewClient(envOr("IMAGE_COMFY_URL", envOr("UGC_COMFY_URL", "http://host.docker.internal:8188")))
	gate := &comfyq.Gate{
		Comfy:      client,
		Schedule:   sched,
		Loc:        loc,
		Cutoff:     time.Duration(envInt("IMAGE_CUTOFF_MINUTES", 5)) * time.Minute,
		ThermalCSV: envOr("IMAGE_THERMAL_CSV", "/ops/thermal.csv"),
		MaxGPUC:    float64(envInt("IMAGE_MAX_GPU_C", 87)),
		MaxZoneC:   float64(envInt("IMAGE_MAX_ZONE_C", 98)),
		CacheTTL:   5 * time.Second,
	}
	q := comfyq.NewQueue(comfyq.Config{
		Priority:    strings.Split(envOr("IMAGE_PRIORITY", "storyteller,webtoon,promoclown,kirian,ugc"), ","),
		JobTimeout:  time.Duration(envInt("IMAGE_JOB_TIMEOUT_MINUTES", 60)) * time.Minute,
		MaxAttempts: envInt("IMAGE_MAX_ATTEMPTS", 5),
		Backoff:     []time.Duration{30 * time.Second, 2 * time.Minute, 5 * time.Minute},
		LostAfter:   90 * time.Second,
		PollEvery:   2 * time.Second,
		IdleEvery:   15 * time.Second,
	}, store, gate, client)
	go q.Run(context.Background())
	(&comfyq.API{
		Q:    q,
		WD14: envOr("IMAGE_WD14_URL", "http://host.docker.internal:8097"),
		HTTP: &http.Client{Timeout: 2 * time.Minute},
	}).Register(mux)
	slog.Info("image api ready", "schedule", sched, "data", envOr("IMAGE_DATA", "/data"))
	return nil
}
