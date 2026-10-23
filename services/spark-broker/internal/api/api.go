// Package api je HTTP rozhraní brokera pro Claude sessions (a curl).
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
	"github.com/ol1n/AiStack/spark-broker/internal/catalog"
)

type API struct {
	B     *broker.Broker
	Cat   *catalog.Catalog
	Token string // volitelný; prázdný = jen LAN bez autentizace (jako audio API)
}

func (a *API) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { write(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("GET /v1/catalog", a.auth(a.catalog))
	mux.HandleFunc("GET /v1/status", a.auth(func(w http.ResponseWriter, r *http.Request) { write(w, 200, a.B.Status()) }))
	mux.HandleFunc("POST /v1/jobs", a.auth(a.submit))
	mux.HandleFunc("GET /v1/jobs", a.auth(func(w http.ResponseWriter, r *http.Request) {
		write(w, 200, a.B.List(r.URL.Query().Get("project"), r.URL.Query().Get("all") == "1"))
	}))
	mux.HandleFunc("GET /v1/jobs/{id}", a.auth(func(w http.ResponseWriter, r *http.Request) {
		j, err := a.B.Get(r.PathValue("id"))
		reply(w, j, err)
	}))
	mux.HandleFunc("PATCH /v1/jobs/{id}", a.auth(func(w http.ResponseWriter, r *http.Request) {
		var p broker.PatchReq
		if !decode(w, r, &p) {
			return
		}
		j, err := a.B.Patch(r.PathValue("id"), p)
		reply(w, j, err)
	}))
	mux.HandleFunc("DELETE /v1/jobs/{id}", a.auth(func(w http.ResponseWriter, r *http.Request) {
		j, err := a.B.Patch(r.PathValue("id"), broker.PatchReq{Cancel: true})
		reply(w, j, err)
	}))
	mux.HandleFunc("POST /v1/jobs/{id}/heartbeat", a.auth(func(w http.ResponseWriter, r *http.Request) {
		var m struct {
			Metrics map[string]broker.Count `json:"metrics"`
		}
		if r.ContentLength > 0 && !decode(w, r, &m) {
			return
		}
		j, err := a.B.Heartbeat(r.PathValue("id"), m.Metrics)
		reply(w, j, err)
	}))
	mux.HandleFunc("POST /v1/jobs/{id}/done", a.auth(func(w http.ResponseWriter, r *http.Request) {
		var d broker.DoneReq
		if r.ContentLength > 0 && !decode(w, r, &d) {
			return
		}
		j, err := a.B.Done(r.PathValue("id"), d)
		reply(w, j, err)
	}))
	mux.HandleFunc("POST /v1/jobs/{id}/extend", a.auth(func(w http.ResponseWriter, r *http.Request) {
		min, _ := strconv.Atoi(r.URL.Query().Get("minutes"))
		j, err := a.B.Extend(r.PathValue("id"), min)
		reply(w, j, err)
	}))
	mux.HandleFunc("POST /v1/jobs/{id}/approve", a.auth(func(w http.ResponseWriter, r *http.Request) {
		j, err := a.B.Approve(r.PathValue("id"))
		reply(w, j, err)
	}))
	mux.HandleFunc("POST /v1/pause", a.auth(func(w http.ResponseWriter, r *http.Request) {
		a.B.SetPaused(true)
		write(w, 200, a.B.Status())
	}))
	mux.HandleFunc("POST /v1/resume", a.auth(func(w http.ResponseWriter, r *http.Request) {
		a.B.SetPaused(false)
		write(w, 200, a.B.Status())
	}))
}

func (a *API) catalog(w http.ResponseWriter, r *http.Request) {
	type prof struct {
		Services []string `json:"services"`
		Addons   []string `json:"addons,omitempty"`
		SwitchMin int     `json:"switch_min,omitempty"`
	}
	out := struct {
		Profiles map[string]prof `json:"profiles"`
		Projects map[string]int  `json:"project_max_priority"`
		Default  int             `json:"default_project_max"`
		Rules    []string        `json:"rules"`
	}{Profiles: map[string]prof{}, Projects: map[string]int{}, Default: a.Cat.DefaultMax}
	for n, p := range a.Cat.Profiles {
		pp := prof{Services: p.Services, SwitchMin: p.SwitchMin}
		for an, ad := range a.Cat.Addons {
			for _, x := range ad.Profiles {
				if x == n {
					pp.Addons = append(pp.Addons, an)
				}
			}
		}
		out.Profiles[n] = pp
	}
	for n, p := range a.Cat.Projects {
		out.Projects[n] = p.Max
	}
	out.Rules = []string{
		"Stage chce služby, které jsou spolu v jednom profilu (nebo jeho doplňky); jinak víc stages.",
		"Priorita 0 = backlog (běží, když nic jiného), strop projektu viz project_max_priority, 100 = okno uživatele.",
		"Okno jménem uživatele: on_behalf_of_user=true + user_quote (doslovná slova uživatele). Ohlásí se na Telegramu.",
		"Lease: posílej POST /v1/jobs/{id}/heartbeat aspoň každých 5 min a na konci POST /v1/jobs/{id}/done s metrikami.",
		"Run: poslední řádek výstupu může být JSON {\"images\":{\"ok\":N,\"failed\":M}} — jde do souhrnu.",
		"Nikdy nespouštěj ani nezastavuj kontejnery a systemd služby na SPARKu sám.",
	}
	write(w, 200, out)
}

func (a *API) submit(w http.ResponseWriter, r *http.Request) {
	var req broker.SubmitReq
	if !decode(w, r, &req) {
		return
	}
	j, err := a.B.Submit(req)
	if err != nil {
		reply(w, nil, err)
		return
	}
	write(w, 201, j)
}

func (a *API) auth(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if a.Token != "" && r.Header.Get("Authorization") != "Bearer "+a.Token {
			write(w, 401, map[string]string{"error": "chybí Authorization: Bearer <token>"})
			return
		}
		h(w, r)
	}
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		write(w, 400, map[string]string{"error": "špatný JSON: " + err.Error()})
		return false
	}
	return true
}

func reply(w http.ResponseWriter, v any, err error) {
	var he *broker.HTTPError
	switch {
	case err == nil:
		write(w, 200, v)
	case errors.Is(err, broker.ErrNotFound):
		write(w, 404, map[string]string{"error": err.Error()})
	case errors.As(err, &he):
		write(w, he.Code, map[string]any{"error": he.Msg, "job": v})
	default:
		write(w, 500, map[string]string{"error": err.Error()})
	}
}

func write(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
