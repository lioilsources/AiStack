package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
)

func testClient(t *testing.T, h http.HandlerFunc) *client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return &client{base: srv.URL, http: srv.Client(), poll: time.Millisecond, log: io.Discard}
}

// 23. 10. bash verze posílala {"metrics":{\}} → 400, skript to nepoznal a broker
// lease po 10 min odepsal. Tělo heartbeatu musí být platný JSON a chyba API error.
func TestHeartbeatBodyAndErrors(t *testing.T) {
	var got map[string]any
	c := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			w.WriteHeader(400)
			w.Write([]byte(`{"error":"neplatný JSON"}`))
			return
		}
		if strings.HasSuffix(r.URL.Path, "/1023-5/heartbeat") {
			w.WriteHeader(409)
			w.Write([]byte(`{"error":"úloha už skončila (orphaned)","job":{"id":"1023-5"}}`))
			return
		}
		w.Write([]byte(`{"id":"1023-8","state":"running"}`))
	})
	if err := c.do("POST", "/v1/jobs/1023-8/heartbeat", map[string]any{"metrics": mustMetrics(t, "")}, nil); err != nil {
		t.Fatalf("heartbeat bez metrik: %v", err)
	}
	if _, ok := got["metrics"]; !ok {
		t.Fatalf("tělo %v bez metrics", got)
	}
	err := c.do("POST", "/v1/jobs/1023-5/heartbeat", map[string]any{"metrics": nil}, nil)
	if err == nil || !strings.Contains(err.Error(), "orphaned") {
		t.Fatalf("osiřelý lease musí být chyba, mám %v", err)
	}
}

func TestMetricsValidation(t *testing.T) {
	m := mustMetrics(t, `{"images":{"ok":44,"failed":2}}`)
	if m["images"].OK != 44 || m["images"].Failed != 2 {
		t.Fatalf("metriky %v", m)
	}
	if _, err := metrics(`{\}`); err == nil {
		t.Fatal("rozbité metriky musí být chyba, ne tichý 400 u brokera")
	}
}

func TestWaitStatesAndReasons(t *testing.T) {
	n := 0
	c := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		n++
		st := map[bool]string{true: "running", false: "queued"}[n >= 3]
		json.NewEncoder(w).Encode(map[string]any{"id": "1", "state": st, "waiting": "potřebuje profil llm"})
	})
	var log bytes.Buffer
	c.log = &log
	if err := c.wait("1"); err != nil {
		t.Fatal(err)
	}
	if strings.Count(log.String(), "čeká:") != 1 {
		t.Fatalf("stejný důvod čekání hlásit jednou:\n%s", log.String())
	}

	c = testClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"id":"2","state":"orphaned","error":"žádný heartbeat"}`))
	})
	if err := c.wait("2"); err == nil {
		t.Fatal("konečný stav při čekání musí být chyba (skript pak skončí)")
	}
}

// Go píše čas bez desetinných míst, když vyjdou nula (00:55:00+02:00) — na tom spadla
// python verze `ls`.
func TestOverviewParsesBrokerJSON(t *testing.T) {
	statusJSON := `{"mode":"control","profile":"llm","paused":false,"probe":{"mem_available_gib":40,"gpu_temp_c":67,"gpu_util":96}}`
	jobsJSON := `[
	 {"id":"1023-6","project":"promoclown","title":"promoclown-heartbeat","priority":90,"state":"running",
	  "stages":[{"services":["qwen36"],"mode":"lease"}],"stage_idx":0,"created":"2026-10-23T17:05:00+02:00",
	  "lease_until":"2026-10-24T00:55:00+02:00"},
	 {"id":"1023-7","project":"storyteller","title":"texty cs","priority":0,"state":"queued",
	  "stages":[{"services":["gptoss"],"mode":"lease"}],"stage_idx":0,"created":"2026-10-23T17:06:01.5+02:00",
	  "waiting":"potřebuje profil gptoss"},
	 {"id":"1023-5","project":"storyteller","title":"scény p2","priority":0,"state":"orphaned",
	  "stages":[{"services":["qwen36"],"mode":"lease"}],"stage_idx":0,"created":"2026-10-23T16:08:47.529999664+02:00",
	  "finished":"2026-10-23T17:07:37.15039396+02:00","error":"žádný heartbeat"}]`
	var st broker.Status
	var jobs []*broker.Job
	if err := json.Unmarshal([]byte(statusJSON), &st); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(jobsJSON), &jobs); err != nil {
		t.Fatal(err)
	}
	now, _ := time.Parse(time.RFC3339, "2026-10-23T17:08:00+02:00")
	var out bytes.Buffer
	overview(&out, st, jobs, now)
	for _, want := range []string{
		"SPARK control: profil llm · volno 40 GiB",
		"running  1023-6   p90    promoclown: promoclown-heartbeat [qwen36] 1/1 · zbývá 7h47",
		"queued   1023-7   p0     storyteller: texty cs [gptoss] 1/1 · čeká: potřebuje profil gptoss",
		"orphaned  1023-5   storyteller: scény p2",
	} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("chybí %q v:\n%s", want, out.String())
		}
	}
}

func TestMinutes(t *testing.T) {
	for in, want := range map[string]int{"3h": 180, "90m": 90, "45": 45} {
		if got, err := minutes(in); err != nil || got != want {
			t.Errorf("minutes(%q) = %d, %v", in, got, err)
		}
	}
	if _, err := minutes("hodinu"); err == nil {
		t.Error("nesmysl musí být chyba")
	}
}

func mustMetrics(t *testing.T, s string) map[string]broker.Count {
	t.Helper()
	m, err := metrics(s)
	if err != nil {
		t.Fatal(err)
	}
	return m
}
