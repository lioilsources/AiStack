package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"time"
)

// envOr returns the environment variable value or a fallback.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// newProxy creates a reverse proxy that streams immediately (required for SSE).
func newProxy(rawURL string) *httputil.ReverseProxy {
	target, err := url.Parse(rawURL)
	if err != nil {
		slog.Error("invalid upstream URL", "url", rawURL, "err", err)
		os.Exit(1)
	}
	p := httputil.NewSingleHostReverseProxy(target)
	p.FlushInterval = -1 // flush each chunk immediately — required for LLM SSE streaming
	p.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		slog.Error("upstream error", "url", rawURL, "path", r.URL.Path, "err", err)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintf(w, `{"error":{"message":"upstream unavailable","type":"gateway_error"}}`)
	}
	return p
}

// --- middleware ---

type ctxKey struct{ name string }

var reqIDKey = ctxKey{"req_id"}

func withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-Id")
		if id == "" {
			id = fmt.Sprintf("%016x", rand.Uint64())
		}
		w.Header().Set("X-Request-Id", id)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), reqIDKey, id)))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (sw *statusWriter) WriteHeader(code int) {
	sw.status = code
	sw.ResponseWriter.WriteHeader(code)
}

func (sw *statusWriter) Unwrap() http.ResponseWriter { return sw.ResponseWriter }

func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		slog.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", sw.status,
			"ms", time.Since(start).Milliseconds(),
			"req_id", r.Context().Value(reqIDKey),
		)
	})
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Request-Id")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// chain wraps h with middlewares applied outermost-first.
func chain(h http.Handler, mw ...func(http.Handler) http.Handler) http.Handler {
	for i := len(mw) - 1; i >= 0; i-- {
		h = mw[i](h)
	}
	return h
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	litellmURL := envOr("LITELLM_URL", "http://litellm:4000")
	ocrAPIURL  := envOr("OCR_API_URL", "http://ocr-api:8003")
	addr       := envOr("LISTEN_ADDR", ":8080")

	llmProxy := newProxy(litellmURL)
	ocrProxy := newProxy(ocrAPIURL)

	mux := http.NewServeMux()

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{ //nolint:errcheck
			"status":  "ok",
			"service": "aistack-gateway",
		})
	})

	// Image generation/editing is handled out-of-band by ComfyUI
	// (comfyui.ol1n.com), not through this gateway.

	// OCR
	mux.Handle("/v1/ocr", ocrProxy)
	mux.Handle("/v1/ocr/", ocrProxy)

	// LLM — OpenAI-compatible: /v1/chat/completions, /v1/completions, /v1/models, …
	mux.Handle("/v1/", llmProxy)
	mux.Handle("/", llmProxy)

	srv := &http.Server{
		Addr:        addr,
		Handler:     chain(mux, withRequestID, withLogging, withCORS),
		ReadTimeout:  0, // disabled — LLM streaming and image generation can take minutes
		WriteTimeout: 0,
		IdleTimeout:  120 * time.Second,
	}

	slog.Info("aistack gateway ready",
		"addr", addr,
		"llm", litellmURL,
		"ocr", ocrAPIURL,
	)

	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("server error", "err", err)
		os.Exit(1)
	}
}
