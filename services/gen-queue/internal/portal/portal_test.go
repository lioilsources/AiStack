package portal

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestProxy(t *testing.T) {
	var seenAuth, seenQuery, seenOrigin string
	comfy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenAuth, seenQuery, seenOrigin = r.Header.Get("Authorization"), r.URL.RawQuery, r.Header.Get("Origin")
		w.Write([]byte("ok")) //nolint:errcheck
	}))
	defer comfy.Close()

	p, err := New(comfy.URL, "s3cret")
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(p)
	defer srv.Close()

	cases := []struct {
		name, path, auth string
		want             int
	}{
		{"bez tokenu", "/system_stats", "", 401},
		{"špatný token", "/system_stats", "Bearer nope", 401},
		{"bearer", "/system_stats", "Bearer s3cret", 200},
		{"holý token", "/system_stats", "s3cret", 200},
		{"query token", "/view?filename=a.png&token=s3cret", "", 200},
		{"špatný query", "/view?token=x", "", 401},
	}
	for _, c := range cases {
		req, _ := http.NewRequest("GET", srv.URL+c.path, nil)
		if c.auth != "" {
			req.Header.Set("Authorization", c.auth)
		}
		req.Header.Set("Origin", "https://evil.example")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != c.want {
			t.Errorf("%s: %d, chci %d", c.name, res.StatusCode, c.want)
		}
	}
	if seenAuth != "" || seenOrigin != "" {
		t.Errorf("do ComfyUI prošlo Authorization=%q Origin=%q", seenAuth, seenOrigin)
	}
	if seenQuery != "filename=a.png" {
		t.Errorf("query do ComfyUI: %q", seenQuery)
	}

	if p, _ := New(comfy.URL, ""); p != nil {
		t.Error("bez tokenu má být proxy vypnutá")
	}
}

func TestComfyDown(t *testing.T) {
	p, _ := New("http://127.0.0.1:1", "t")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/system_stats", nil)
	req.Header.Set("Authorization", "Bearer t")
	p.ServeHTTP(rec, req)
	if rec.Code != 503 {
		t.Errorf("ComfyUI dole: %d, chci 503", rec.Code)
	}
}

func TestSaveName(t *testing.T) {
	var seenBody string
	comfy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		seenBody = string(b)
	}))
	defer comfy.Close()
	p, _ := New(comfy.URL, "s3cret")
	srv := httptest.NewServer(p)
	defer srv.Close()

	cases := []struct {
		body string
		want int
	}{
		{`{"workflow":"{}","name":"moje.json"}`, 200},
		{`{"workflow":"{}"}`, 200},
		{`{"workflow":"{}","name":"../../x"}`, 400},
		{`{"workflow":"{}","name":"/home/ol1n/x"}`, 400},
		{`{"workflow":"{}","name":"..\\x"}`, 400},
		{`{"workflow":"{}","name":".bashrc"}`, 400},
		{`{"workflow":"{}","name":""}`, 400},
		{`nejson`, 400},
	}
	for _, c := range cases {
		seenBody = ""
		req, _ := http.NewRequest("POST", srv.URL+"/cpe/workflow/save", strings.NewReader(c.body))
		req.Header.Set("Authorization", "Bearer s3cret")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != c.want {
			t.Errorf("%s: %d, chci %d", c.body, res.StatusCode, c.want)
		}
		if c.want == 200 && seenBody != c.body {
			t.Errorf("tělo do ComfyUI: %q", seenBody)
		}
		if c.want != 200 && seenBody != "" {
			t.Errorf("%s prošlo do ComfyUI", c.body)
		}
	}
}
