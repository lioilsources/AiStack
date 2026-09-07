package audioclient

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestGenerateMusicSubmitsSpec(t *testing.T) {
	var got MusicSpec
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/audio/music" {
			t.Errorf("path = %q, want /v1/audio/music", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.WriteHeader(http.StatusAccepted)
		json.NewEncoder(w).Encode(map[string]any{"job_id": "abc", "queue_position": 2}) //nolint:errcheck
	}))
	defer srv.Close()

	seed := int64(42)
	job, err := New(srv.URL).GenerateMusic(context.Background(), MusicSpec{
		Prompt: "chiptune boss theme", DurationSec: 40, Seed: &seed,
		Instrumental: true, BPM: 140, Loop: true, Format: "ogg",
	})
	if err != nil {
		t.Fatalf("GenerateMusic: %v", err)
	}
	if job.JobID != "abc" || job.Status != "queued" {
		t.Errorf("job = %+v", job)
	}
	if job.QueuePosition == nil || *job.QueuePosition != 2 {
		t.Errorf("queue position = %v, want 2", job.QueuePosition)
	}
	if got.BPM != 140 || got.Seed == nil || *got.Seed != 42 || !got.Loop {
		t.Errorf("odeslaný spec = %+v", got)
	}
}

func TestGenerateSFXOmitsSeedWhenUnset(t *testing.T) {
	var raw map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&raw) //nolint:errcheck
		w.WriteHeader(http.StatusAccepted)
		json.NewEncoder(w).Encode(map[string]any{"job_id": "s1"}) //nolint:errcheck
	}))
	defer srv.Close()

	if _, err := New(srv.URL).GenerateSFX(context.Background(), SFXSpec{
		Prompt: "laser", DurationSec: 0.5, Mono: true,
	}); err != nil {
		t.Fatalf("GenerateSFX: %v", err)
	}
	// Seed nesmí přijít jako 0 — služba by ho vzala jako platný a všechny
	// varianty by vyšly stejně.
	if _, present := raw["seed"]; present {
		t.Errorf("seed odeslán i když nebyl nastaven: %v", raw)
	}
}

func TestWaitJobPollsUntilDone(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		status := "running"
		outputs := []Output{}
		if n >= 3 {
			status = "done"
			outputs = []Output{{URL: "/v1/audio/jobs/j/outputs/00.ogg", Filename: "00.ogg", Seed: 7}}
		}
		json.NewEncoder(w).Encode(Job{JobID: "j", Status: status, Outputs: outputs}) //nolint:errcheck
	}))
	defer srv.Close()

	c := New(srv.URL, WithPollInterval(5*time.Millisecond))
	job, err := c.WaitJob(context.Background(), "j", 5*time.Second)
	if err != nil {
		t.Fatalf("WaitJob: %v", err)
	}
	if len(job.Outputs) != 1 || job.Outputs[0].Seed != 7 {
		t.Errorf("outputs = %+v", job.Outputs)
	}
	if calls.Load() < 3 {
		t.Errorf("volání = %d, čekal aspoň 3", calls.Load())
	}
}

func TestWaitJobReturnsBackendError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		json.NewEncoder(w).Encode(Job{JobID: "j", Status: "error", Error: "model spadl"}) //nolint:errcheck
	}))
	defer srv.Close()

	_, err := New(srv.URL, WithPollInterval(time.Millisecond)).WaitJob(context.Background(), "j", time.Second)
	if err == nil || !strings.Contains(err.Error(), "model spadl") {
		t.Fatalf("err = %v, čekal chybu s hláškou backendu", err)
	}
}

func TestWaitJobSurvivesTransientNetworkError(t *testing.T) {
	// Restart audio kontejneru uprostřed dlouhé generace nesmí zahodit job,
	// který za pár sekund doběhne.
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		json.NewEncoder(w).Encode(Job{JobID: "j", Status: "done"}) //nolint:errcheck
	}))
	defer srv.Close()

	if _, err := New(srv.URL, WithPollInterval(time.Millisecond)).
		WaitJob(context.Background(), "j", 2*time.Second); err != nil {
		t.Fatalf("WaitJob: %v", err)
	}
}

func TestWaitJobRespectsTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		json.NewEncoder(w).Encode(Job{JobID: "j", Status: "running"}) //nolint:errcheck
	}))
	defer srv.Close()

	_, err := New(srv.URL, WithPollInterval(time.Millisecond)).
		WaitJob(context.Background(), "j", 50*time.Millisecond)
	if err == nil {
		t.Fatal("čekal timeout")
	}
}

func TestDownloadWritesBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer k" {
			t.Errorf("chybí klíč: %q", r.Header.Get("Authorization"))
		}
		w.Write([]byte("OggS-fake")) //nolint:errcheck
	}))
	defer srv.Close()

	var buf bytes.Buffer
	n, err := New(srv.URL, WithAPIKey("k")).Download(context.Background(),
		Output{URL: "/v1/audio/jobs/j/outputs/00.ogg"}, &buf)
	if err != nil {
		t.Fatalf("Download: %v", err)
	}
	if n != int64(buf.Len()) || buf.String() != "OggS-fake" {
		t.Errorf("stáhnuto %q (%d B)", buf.String(), n)
	}
}

func TestAPIErrorCarriesStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"detail":"backend pro sfx není nakonfigurován"}`, http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	_, err := New(srv.URL).GenerateSFX(context.Background(), SFXSpec{Prompt: "x", DurationSec: 1})
	var apiErr *APIError
	if !errorsAs(err, &apiErr) || apiErr.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("err = %v, čekal APIError 503", err)
	}
}

func TestModelsCarryLicense(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		json.NewEncoder(w).Encode([]Model{{ //nolint:errcheck
			Name: "acestep-v15-turbo", Kind: "music", License: "MIT",
			Commercial: true, Available: true,
		}}) //nolint:errcheck
	}))
	defer srv.Close()

	models, err := New(srv.URL).Models(context.Background())
	if err != nil {
		t.Fatalf("Models: %v", err)
	}
	if len(models) != 1 || models[0].License != "MIT" || !models[0].Commercial {
		t.Errorf("models = %+v", models)
	}
}

// errorsAs je errors.As bez importu v testu (Go 1.23 ho má, jen zkracuje zápis).
func errorsAs(err error, target **APIError) bool {
	for err != nil {
		if e, ok := err.(*APIError); ok {
			*target = e
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}
