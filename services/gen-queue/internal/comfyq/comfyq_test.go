package comfyq

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeComfy is just enough ComfyUI: system_stats, queue, upload, prompt,
// history, view, interrupt, object_info.
type fakeComfy struct {
	mu        sync.Mutex
	down      bool
	cpu       bool
	foreign   int
	reject    bool // node_errors on /prompt
	hang      bool // never complete
	uploads   map[string][]byte
	prompts   []string
	completed map[string]bool
	interrupt int
}

func newFake() *fakeComfy {
	return &fakeComfy{uploads: map[string][]byte{}, completed: map[string]bool{}}
}

func (f *fakeComfy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		http.Error(w, "down", http.StatusBadGateway)
		return
	}
	switch {
	case r.URL.Path == "/system_stats":
		typ := "cuda"
		if f.cpu {
			typ = "cpu"
		}
		fmt.Fprintf(w, `{"devices":[{"type":%q}]}`, typ)
	case r.URL.Path == "/queue":
		var running []any
		for _, p := range f.prompts {
			if !f.completed[p] && !f.hang {
				running = append(running, []any{0, p})
			}
		}
		for i := 0; i < f.foreign; i++ {
			running = append(running, []any{0, "foreign"})
		}
		json.NewEncoder(w).Encode(map[string]any{"queue_running": running, "queue_pending": []any{}})
	case r.URL.Path == "/upload/image":
		file, hdr, _ := r.FormFile("image")
		data, _ := io.ReadAll(file)
		f.uploads[hdr.Filename] = data
		w.Write([]byte(`{}`))
	case r.URL.Path == "/prompt":
		if f.reject {
			w.WriteHeader(400)
			w.Write([]byte(`{"error":{"type":"prompt_outputs_failed_validation"},"node_errors":{"3":{"errors":["bad"]}}}`))
			return
		}
		id := fmt.Sprintf("p%d", len(f.prompts)+1)
		f.prompts = append(f.prompts, id)
		fmt.Fprintf(w, `{"prompt_id":%q,"node_errors":{}}`, id)
	case strings.HasPrefix(r.URL.Path, "/history/"):
		id := strings.TrimPrefix(r.URL.Path, "/history/")
		if f.hang {
			w.Write([]byte(`{}`))
			return
		}
		f.completed[id] = true
		fmt.Fprintf(w, `{%q:{"status":{"status_str":"success","completed":true,"messages":[]},
			"outputs":{"9":{"images":[{"filename":"a.png","subfolder":"","type":"output"},
			{"filename":"prev.png","subfolder":"","type":"temp"}]},
			"12":{"gifs":[{"filename":"b.mp4","subfolder":"vid","type":"output"}]}}}}`, id)
	case r.URL.Path == "/view":
		fmt.Fprintf(w, "DATA:%s/%s", r.URL.Query().Get("subfolder"), r.URL.Query().Get("filename"))
	case r.URL.Path == "/interrupt":
		f.interrupt++
	case strings.HasPrefix(r.URL.Path, "/object_info/"):
		node := strings.TrimPrefix(r.URL.Path, "/object_info/")
		fmt.Fprintf(w, `{%q:{"input":{"required":{"ckpt_name":[["x.safetensors"]],"lora_name":["COMBO",{"options":["l.safetensors"]}],"vae_name":[["v.safetensors"]],"unet_name":[["u.safetensors"]]}}}}`, node)
	default:
		http.NotFound(w, r)
	}
}

func (f *fakeComfy) set(fn func(*fakeComfy)) {
	f.mu.Lock()
	fn(f)
	f.mu.Unlock()
}

type env struct {
	t     *testing.T
	fake  *fakeComfy
	q     *Queue
	srv   *httptest.Server
	dir   string
	clock time.Time
}

func setup(t *testing.T) *env {
	t.Helper()
	fake := newFake()
	cs := httptest.NewServer(fake)
	t.Cleanup(cs.Close)
	wd14 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		fmt.Fprintf(w, `{"path":%q,"bytes":%d}`, r.URL.Path, len(b))
	}))
	t.Cleanup(wd14.Close)
	dir := t.TempDir()
	store, err := OpenStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	loc, _ := time.LoadLocation("Europe/Prague")
	sched, _ := ParseSchedule("7:comfy 13:llm")
	e := &env{t: t, fake: fake, dir: dir, clock: time.Date(2026, 10, 19, 10, 0, 0, 0, loc)}
	client := NewClient(cs.URL)
	gate := &Gate{Comfy: client, Schedule: sched, Loc: loc, Cutoff: 5 * time.Minute,
		MaxGPUC: 87, MaxZoneC: 98, Now: func() time.Time { return e.clock }}
	e.q = NewQueue(Config{
		Priority: []string{"storyteller", "webtoon"}, JobTimeout: 5 * time.Second, MaxAttempts: 3,
		Backoff: []time.Duration{0}, LostAfter: 200 * time.Millisecond,
		PollEvery: 10 * time.Millisecond, IdleEvery: 20 * time.Millisecond,
	}, store, gate, client)
	mux := http.NewServeMux()
	(&API{Q: e.q, WD14: wd14.URL, HTTP: http.DefaultClient}).Register(mux)
	e.srv = httptest.NewServer(mux)
	t.Cleanup(e.srv.Close)
	return e
}

func (e *env) do(method, path, project string, body any) (int, map[string]any) {
	e.t.Helper()
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, e.srv.URL+path, rd)
	if project != "" {
		req.Header.Set("X-Project", project)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	json.NewDecoder(resp.Body).Decode(&m)
	return resp.StatusCode, m
}

var wf = json.RawMessage(`{"3":{"class_type":"KSampler","inputs":{"seed":1}},"5":{"class_type":"EmptyLatentImage","inputs":{"width":1024,"height":1024}},"9":{"class_type":"SaveImage","inputs":{}}}`)

func (e *env) submit(project, ref string) string {
	e.t.Helper()
	code, m := e.do("POST", "/v1/image/jobs", project, map[string]any{
		"workflow": wf, "client_ref": ref,
		"inputs": map[string]string{"ref.png": base64.StdEncoding.EncodeToString([]byte("PNG"))},
	})
	if code != http.StatusAccepted && code != http.StatusOK {
		e.t.Fatalf("submit: %d %v", code, m)
	}
	return m["id"].(string)
}

func (e *env) runUntil(id string, want Status) Job {
	e.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go e.q.Run(ctx)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if j, _ := e.q.store.Get(id); j.Status == want {
			return j
		}
		time.Sleep(10 * time.Millisecond)
	}
	j, _ := e.q.store.Get(id)
	e.t.Fatalf("job %s: status %s (%s), want %s", id, j.Status, j.Error, want)
	return j
}

func TestJobRoundTrip(t *testing.T) {
	e := setup(t)
	id := e.submit("webtoon", "panel-1")
	j := e.runUntil(id, StatusDone)
	if string(e.fake.uploads["ref.png"]) != "PNG" {
		t.Fatalf("input not uploaded: %v", e.fake.uploads)
	}
	if len(j.Outputs) != 2 || !strings.HasPrefix(j.Outputs[0], filepath.Join("out", "webtoon", "2026-")) &&
		!strings.HasPrefix(j.Outputs[0], filepath.Join("out", "webtoon")) {
		t.Fatalf("outputs: %v", j.Outputs)
	}
	resp, _ := http.Get(e.srv.URL + "/v1/image/jobs/" + id + "/outputs/1")
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "DATA:vid/b.mp4" || resp.Header.Get("Content-Type") != "video/mp4" {
		t.Fatalf("output 1: %q %s", body, resp.Header.Get("Content-Type"))
	}
	code, m := e.do("GET", "/v1/image/jobs/"+id, "", nil)
	if code != 200 || m["status"] != "done" || m["n_outputs"].(float64) != 2 || m["outputs"] != nil {
		t.Fatalf("status: %d %v", code, m)
	}
	// Idempotent resubmit returns the same job.
	if again := e.submit("webtoon", "panel-1"); again != id {
		t.Fatalf("client_ref not idempotent: %s vs %s", again, id)
	}
	// Restart: the store reloads the job from disk.
	s2, err := OpenStore(e.dir)
	if err != nil {
		t.Fatal(err)
	}
	if j2, ok := s2.Get(id); !ok || j2.Status != StatusDone {
		t.Fatalf("reload: %v %v", ok, j2.Status)
	}
}

func TestInvalidWorkflowErrorsWithoutRetry(t *testing.T) {
	e := setup(t)
	e.fake.set(func(f *fakeComfy) { f.reject = true })
	id := e.submit("webtoon", "")
	j := e.runUntil(id, StatusError)
	if j.Attempts != 1 || !strings.Contains(j.Error, "node_errors") {
		t.Fatalf("attempts %d, err %q", j.Attempts, j.Error)
	}
}

func TestWaitsForWindow(t *testing.T) {
	e := setup(t)
	e.fake.set(func(f *fakeComfy) { f.down = true })
	e.clock = e.clock.Add(5 * time.Hour) // 15:00, llm profile
	id := e.submit("webtoon", "")
	_, m := e.do("GET", "/v1/image/jobs/"+id, "", nil)
	win := m["window"].(map[string]any)
	if m["status"] != "waiting_window" || win["state"] != "closed" || !strings.Contains(win["next_open"].(string), "T07:00:00") {
		t.Fatalf("closed window: %v", m)
	}
	// Window opens: ComfyUI comes up, scheduler says comfy.
	e.clock = e.clock.Add(17 * time.Hour) // next day 08:00
	e.fake.set(func(f *fakeComfy) { f.down = false })
	e.q.gate.Invalidate()
	e.runUntil(id, StatusDone)
}

func TestGateStates(t *testing.T) {
	e := setup(t)
	ctx := context.Background()
	check := func(want string) {
		t.Helper()
		e.q.gate.Invalidate()
		if st := e.q.gate.Check(ctx); st.State != want {
			t.Fatalf("gate %s (%s), want %s", st.State, st.Reason, want)
		}
	}
	check("open")
	e.fake.set(func(f *fakeComfy) { f.foreign = 1 })
	check("busy")
	e.fake.set(func(f *fakeComfy) { f.foreign = 0; f.cpu = true })
	check("cpu")
	e.fake.set(func(f *fakeComfy) { f.cpu = false })
	e.clock = time.Date(2026, 10, 19, 12, 57, 0, 0, e.q.gate.Loc) // 3 min before 13:00 llm
	check("closed")
	e.clock = time.Date(2026, 10, 19, 12, 0, 0, 0, e.q.gate.Loc)
	csv := filepath.Join(e.dir, "thermal.csv")
	os.WriteFile(csv, []byte("ts,gpu_temp_c,a,b,c,d,e,f,zone_max_c\n2026/10/19 12:00:00,88,0,0,0,0,0,0,80\n"), 0o644)
	os.Chtimes(csv, e.clock, e.clock) // fresh relative to the gate clock
	e.q.gate.ThermalCSV = csv
	check("hot")
	e.clock = e.clock.Add(3 * time.Minute) // logger died: stale line is ignored
	check("open")
}

func TestPriorityAndCancel(t *testing.T) {
	e := setup(t)
	w := e.submit("webtoon", "")
	s := e.submit("storyteller", "")
	if e.q.Position(s) != 0 || e.q.Position(w) != 1 {
		t.Fatalf("storyteller must go first: s=%d w=%d", e.q.Position(s), e.q.Position(w))
	}
	if code, _ := e.do("DELETE", "/v1/image/jobs/"+w, "storyteller", nil); code != http.StatusForbidden {
		t.Fatalf("foreign cancel: %d", code)
	}
	if code, m := e.do("DELETE", "/v1/image/jobs/"+w, "webtoon", nil); code != 200 || m["status"] != "cancelled" {
		t.Fatalf("own cancel: %d %v", code, m)
	}
	// A running job of our own: interrupted in ComfyUI.
	e.fake.set(func(f *fakeComfy) { f.hang = true })
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go e.q.Run(ctx)
	deadline := time.Now().Add(2 * time.Second)
	for {
		if j, _ := e.q.store.Get(s); j.PromptID != "" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("job never started")
		}
		time.Sleep(5 * time.Millisecond)
	}
	e.fake.set(func(f *fakeComfy) { f.hang = false }) // so /queue reports our prompt as running
	if code, m := e.do("DELETE", "/v1/image/jobs/"+s, "storyteller", nil); code != 200 || m["status"] != "cancelled" {
		t.Fatalf("running cancel: %d %v", code, m)
	}
	time.Sleep(50 * time.Millisecond)
	if j, _ := e.q.store.Get(s); j.Status != StatusCancelled {
		t.Fatalf("worker overwrote cancel: %s", j.Status)
	}
}

func TestComfyLostRequeues(t *testing.T) {
	e := setup(t)
	e.fake.set(func(f *fakeComfy) { f.hang = true })
	id := e.submit("webtoon", "")
	ctx, cancel := context.WithCancel(context.Background())
	go e.q.Run(ctx)
	for {
		if j, _ := e.q.store.Get(id); j.PromptID != "" {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	e.fake.set(func(f *fakeComfy) { f.down = true }) // scheduler stops ComfyUI mid-run
	time.Sleep(400 * time.Millisecond)
	cancel()
	j, _ := e.q.store.Get(id)
	if j.Status != StatusQueued || !strings.Contains(j.Error, "lost") {
		t.Fatalf("want requeued after loss, got %s %q", j.Status, j.Error)
	}
}

func TestValidationAndProxies(t *testing.T) {
	e := setup(t)
	if code, _ := e.do("POST", "/v1/image/jobs", "", map[string]any{"workflow": wf}); code != 400 {
		t.Fatalf("missing X-Project: %d", code)
	}
	big := json.RawMessage(`{"5":{"class_type":"EmptyLatentImage","inputs":{"width":4096,"height":1024}}}`)
	if code, m := e.do("POST", "/v1/image/jobs", "webtoon", map[string]any{"workflow": big}); code != 400 {
		t.Fatalf("4096 px accepted: %d %v", code, m)
	}
	ui := json.RawMessage(`{"nodes":[],"links":[]}`)
	if code, _ := e.do("POST", "/v1/image/jobs", "webtoon", map[string]any{"workflow": ui}); code != 400 {
		t.Fatalf("UI-format workflow accepted: %d", code)
	}
	if code, _ := e.do("POST", "/v1/image/jobs", "webtoon", map[string]any{"workflow": wf,
		"inputs": map[string]string{"../x.png": "UE5H"}}); code != 400 {
		t.Fatalf("path traversal input accepted: %d", code)
	}
	resp, err := http.Post(e.srv.URL+"/v1/image/tag?threshold=0.4", "image/png", strings.NewReader("PNGDATA"))
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(b), `"bytes":7`) {
		t.Fatalf("tag proxy: %s", b)
	}
	code, m := e.do("GET", "/v1/image/models", "", nil)
	models := m["models"].(map[string]any)
	if code != 200 || models["loras"].([]any)[0] != "l.safetensors" || models["checkpoints"].([]any)[0] != "x.safetensors" {
		t.Fatalf("models: %d %v", code, m)
	}
	code, m = e.do("GET", "/v1/image/status", "", nil)
	if code != 200 || m["window"].(map[string]any)["state"] != "open" {
		t.Fatalf("status: %d %v", code, m)
	}
}
