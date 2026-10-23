// spark-broker — fronta úloh pro SPARK: okno na vyžádání, jinak podle priority.
//
//	BROKER_CONFIG=~/spark-broker/config.yaml BROKER_MODE=advise|control spark-broker
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/api"
	"github.com/ol1n/AiStack/spark-broker/internal/broker"
	"github.com/ol1n/AiStack/spark-broker/internal/catalog"
	"github.com/ol1n/AiStack/spark-broker/internal/notify"
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, nil)))
	home, _ := os.UserHomeDir()
	cfgPath := env("BROKER_CONFIG", home+"/spark-broker/config.yaml")
	cat, err := catalog.Load(cfgPath)
	if err != nil {
		slog.Error("katalog", "path", cfgPath, "err", err)
		os.Exit(1)
	}
	mode := env("BROKER_MODE", "advise")
	control := mode == "control"
	if control {
		// V control režimu smí SPARK přepínat jen broker. Běžící rag-schedule.timer
		// by ho přepínal pod rukama (stejná chyba jako 23. 10. 08:03).
		if exec.Command("systemctl", "--user", "is-active", "--quiet", "rag-schedule.timer").Run() == nil {
			slog.Error("rag-schedule.timer běží — nejdřív `systemctl --user disable --now rag-schedule.timer`, nebo BROKER_MODE=advise")
			os.Exit(1)
		}
	}
	nt := notify.Cmd{Path: expand(cat.NotifyCmd, home)}
	b, err := broker.New(cat, &broker.Host{ComfyURL: cat.Comfy}, nt, broker.Options{
		StatePath: env("BROKER_STATE", home+"/spark-broker/state.json"),
		Control:   control,
		Async:     true,
	})
	if err != nil {
		slog.Error("stav", "err", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go b.Run(ctx, 15*time.Second)

	mux := http.NewServeMux()
	(&api.API{B: b, Cat: cat, Token: os.Getenv("BROKER_TOKEN")}).Register(mux)
	addr := env("BROKER_ADDR", ":8100")
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	slog.Info("spark-broker", "addr", addr, "mode", mode, "config", cfgPath)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("http", "err", err)
		os.Exit(1)
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func expand(p, home string) string {
	if strings.HasPrefix(p, "~/") {
		return home + p[1:]
	}
	return p
}
