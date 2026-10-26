package main

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
)

func TestParseOpts(t *testing.T) {
	rest, o, err := parseOpts([]string{"lease", "--after", "1024-14,1024-15", "webtoon", "--id-file", "/tmp/x", "comfyui", "60", "--", "echo", "--after"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(rest, " ") != "lease webtoon comfyui 60" || strings.Join(o.after, ",") != "1024-14,1024-15" ||
		o.idFile != "/tmp/x" || strings.Join(o.cmd, " ") != "echo --after" {
		t.Fatalf("rest %v opts %+v", rest, o)
	}
	if _, _, err := parseOpts([]string{"hold", "1", "--proc"}); err == nil {
		t.Fatal("--proc bez hodnoty musí být chyba")
	}
}

// fakeBroker drží stav jedné úlohy a zapisuje, co klient volal.
type fakeBroker struct {
	mu    sync.Mutex
	state string
	calls []string
}

func (f *fakeBroker) handler(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := strings.TrimPrefix(r.URL.Path, "/v1/jobs")
	f.calls = append(f.calls, r.Method+" "+path)
	switch {
	case r.Method == "POST" && path == "":
		var req broker.SubmitReq
		json.NewDecoder(r.Body).Decode(&req)
		f.calls = append(f.calls, "after="+strings.Join(req.After, ","))
	case strings.HasSuffix(path, "/done"):
		var d broker.DoneReq
		json.NewDecoder(r.Body).Decode(&d)
		f.state = broker.Done
		if d.Error != "" {
			f.state = broker.Failed
		}
		if n := d.Metrics["images"].OK; n > 0 {
			f.calls = append(f.calls, "images ok")
		}
	}
	json.NewEncoder(w).Encode(map[string]any{"id": "7", "state": f.state})
}

func (f *fakeBroker) saw(s string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.calls {
		if strings.Contains(c, s) {
			return true
		}
	}
	return false
}

func TestHoldReleasesAfterProcessEnds(t *testing.T) {
	f := &fakeBroker{state: broker.Running}
	c := testClient(t, f.handler)
	n := 0
	procRunning = func(string) bool { n++; return n <= 2 } // běží dva tiky, pak skončí
	defer func() { procRunning = func(p string) bool { return false } }()
	if err := c.hold("7", opts{proc: "local_strip.sh"}); err != nil {
		t.Fatal(err)
	}
	if !f.saw("POST /7/heartbeat") || !f.saw("POST /7/done") {
		t.Fatalf("volání %v", f.calls)
	}
}

func TestExecRunsCommandAndReportsMetrics(t *testing.T) {
	f := &fakeBroker{state: broker.Running}
	c := testClient(t, f.handler)
	r := broker.SubmitReq{Project: "storyteller", After: []string{"1024-15"}}
	err := c.execJob(r, opts{cmd: []string{"sh", "-c", `sleep 0.05; echo '{"images":{"ok":3,"failed":0}}'`}})
	if err != nil {
		t.Fatal(err)
	}
	if !f.saw("after=1024-15") || !f.saw("images ok") {
		t.Fatalf("volání %v", f.calls)
	}
	f.state = broker.Running
	if err := c.execJob(r, opts{cmd: []string{"sh", "-c", "exit 3"}}); err == nil || f.state != broker.Failed {
		t.Fatalf("nenulový exit musí být fail: %v %s", err, f.state)
	}
}

func TestParseAt(t *testing.T) {
	now := time.Date(2026, 10, 26, 13, 0, 0, 0, time.Local)
	for in, want := range map[string]string{
		"19:05":            "2026-10-26 19:05",
		"06:30":            "2026-10-27 06:30", // už minulo → zítra
		"2026-10-28 08:00": "2026-10-28 08:00",
		"+90m":             "2026-10-26 14:30",
		"+2h":              "2026-10-26 15:00",
	} {
		got, err := parseAt(in, now)
		if err != nil || got.Format("2006-01-02 15:04") != want {
			t.Errorf("%s → %v %v, čekal jsem %s", in, got, err, want)
		}
	}
	if _, err := parseAt("večer", now); err == nil {
		t.Error("nesmysl musí selhat")
	}
}
