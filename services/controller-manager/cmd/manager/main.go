package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ol1n/AiStack/controller-manager/internal/api"
	"github.com/ol1n/AiStack/controller-manager/internal/compose"
	"github.com/ol1n/AiStack/controller-manager/internal/config"
	"github.com/ol1n/AiStack/controller-manager/internal/health"
	"github.com/ol1n/AiStack/controller-manager/internal/state"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	cfgPath := os.Getenv("INFERENCE_CONFIG")
	cfg, err := config.Load(cfgPath)
	if err != nil {
		slog.Error("load config", "err", err)
		os.Exit(1)
	}

	statePath := os.Getenv("INFERENCE_STATE")
	store := state.New(statePath)

	// Verify active model is actually running on startup.
	st, err := store.Read()
	if err != nil {
		slog.Warn("read state", "err", err)
	} else if st.Active != "" {
		model, ok := cfg.Models[st.Active]
		if ok {
			slog.Info("verifying active model", "model", st.Active)
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			if err := health.WaitHealthy(ctx, model.HealthURL, time.Second, 10*time.Second); err != nil {
				slog.Warn("active model not healthy, clearing state", "model", st.Active, "err", err)
				_ = store.Write(state.State{Active: "", Healthy: false, Since: time.Now()})
			} else {
				slog.Info("active model healthy", "model", st.Active)
			}
			cancel()
		}
	}

	runner := compose.New()
	h := api.New(cfg, store, runner)
	mux := http.NewServeMux()
	h.Register(mux)

	addr := os.Getenv("INFERENCE_ADDR")
	if addr == "" {
		addr = ":8090"
	}

	srv := &http.Server{
		Addr:    addr,
		Handler: mux,
	}

	done := make(chan os.Signal, 1)
	signal.Notify(done, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		slog.Info("starting controller-manager", "addr", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server error", "err", err)
			os.Exit(1)
		}
	}()

	<-done
	slog.Info("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
