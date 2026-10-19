package comfyq

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"time"
)

const (
	maxWorkflow  = 1 << 20
	maxInputs    = 20 << 20
	maxSide      = 2048
	maxTagUpload = 20 << 20
)

var (
	projectRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,31}$`)
	inputRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
)

type API struct {
	Q      *Queue
	WD14   string // e.g. http://host.docker.internal:8097
	HTTP   *http.Client
	models struct {
		sync.Mutex
		last map[string][]string
		at   time.Time
	}
}

func (a *API) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/image/jobs", a.submit)
	mux.HandleFunc("GET /v1/image/jobs/{id}", a.status)
	mux.HandleFunc("GET /v1/image/jobs/{id}/outputs/{n}", a.output)
	mux.HandleFunc("DELETE /v1/image/jobs/{id}", a.cancel)
	mux.HandleFunc("POST /v1/image/tag", a.tag)
	mux.HandleFunc("GET /v1/image/status", a.queueStatus)
	mux.HandleFunc("GET /v1/image/models", a.listModels)
}

type submitReq struct {
	Workflow  json.RawMessage   `json:"workflow"`
	Inputs    map[string]string `json:"inputs"` // name -> base64
	ClientRef string            `json:"client_ref"`
}

func project(r *http.Request) (string, error) {
	p := r.Header.Get("X-Project")
	if !projectRe.MatchString(p) {
		return "", errors.New("missing or invalid X-Project header (a-z, 0-9, -)")
	}
	return p, nil
}

func (a *API) submit(w http.ResponseWriter, r *http.Request) {
	proj, err := project(r)
	if err != nil {
		jsonErr(w, err.Error(), http.StatusBadRequest)
		return
	}
	// base64 inflates by 4/3; leave room for the JSON around it.
	r.Body = http.MaxBytesReader(w, r.Body, maxWorkflow+maxInputs*4/3+64<<10)
	var req submitReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonErr(w, "bad JSON: "+err.Error(), http.StatusBadRequest)
		return
	}
	if len(req.Workflow) == 0 || len(req.Workflow) > maxWorkflow {
		jsonErr(w, fmt.Sprintf("workflow missing or over %d bytes", maxWorkflow), http.StatusBadRequest)
		return
	}
	if err := checkWorkflow(req.Workflow); err != nil {
		jsonErr(w, err.Error(), http.StatusBadRequest)
		return
	}
	if len(req.ClientRef) > 200 {
		jsonErr(w, "client_ref over 200 chars", http.StatusBadRequest)
		return
	}
	inputs := map[string][]byte{}
	total := 0
	for name, b64 := range req.Inputs {
		if !inputRe.MatchString(name) {
			jsonErr(w, fmt.Sprintf("bad input name %q", name), http.StatusBadRequest)
			return
		}
		data, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			jsonErr(w, fmt.Sprintf("input %s: bad base64", name), http.StatusBadRequest)
			return
		}
		total += len(data)
		inputs[name] = data
	}
	if total > maxInputs {
		jsonErr(w, fmt.Sprintf("inputs over %d bytes", maxInputs), http.StatusRequestEntityTooLarge)
		return
	}
	j := &Job{ID: newID(), Project: proj, ClientRef: req.ClientRef, Status: StatusQueued, CreatedAt: time.Now()}
	saved, existed, err := a.Q.store.Add(j, req.Workflow, inputs)
	if err != nil {
		slog.Error("image job store", "err", err)
		jsonErr(w, "store failed", http.StatusInternalServerError)
		return
	}
	code := http.StatusAccepted
	if existed {
		code = http.StatusOK
	} else {
		slog.Info("image job submitted", "id", saved.ID, "project", proj, "inputs", len(inputs))
		a.Q.poke()
	}
	writeJSON(w, code, a.view(r, saved))
}

// checkWorkflow wants the ComfyUI API format (node id -> {class_type, inputs})
// and rejects width/height above maxSide.
func checkWorkflow(raw json.RawMessage) error {
	var nodes map[string]struct {
		ClassType string         `json:"class_type"`
		Inputs    map[string]any `json:"inputs"`
	}
	if err := json.Unmarshal(raw, &nodes); err != nil || len(nodes) == 0 {
		return errors.New("workflow must be ComfyUI API format: {node_id: {class_type, inputs}}")
	}
	for id, n := range nodes {
		if n.ClassType == "" {
			return fmt.Errorf("node %s: missing class_type (UI export? use 'Save (API)')", id)
		}
		for _, k := range []string{"width", "height"} {
			if v, ok := n.Inputs[k].(float64); ok && v > maxSide {
				return fmt.Errorf("node %s: %s %.0f over %d px", id, k, v, maxSide)
			}
		}
	}
	return nil
}

type jobView struct {
	Job
	Position int       `json:"position"`
	Window   GateState `json:"window"`
	NOutputs int       `json:"n_outputs"`
}

func (a *API) view(r *http.Request, j Job) jobView {
	v := jobView{Job: j, Position: -1, NOutputs: len(j.Outputs)}
	v.Window = a.Q.gate.Check(r.Context())
	if j.Status == StatusQueued {
		v.Position = a.Q.Position(j.ID)
		if !v.Window.Open() {
			v.Status = StatusWaitingWindow
		}
	}
	// Paths on the SPARK disk are not the client's business.
	v.Outputs = nil
	return v
}

func (a *API) status(w http.ResponseWriter, r *http.Request) {
	j, ok := a.Q.store.Get(r.PathValue("id"))
	if !ok {
		jsonErr(w, "job not found", http.StatusNotFound)
		return
	}
	writeJSON(w, http.StatusOK, a.view(r, j))
}

func (a *API) output(w http.ResponseWriter, r *http.Request) {
	j, ok := a.Q.store.Get(r.PathValue("id"))
	if !ok {
		jsonErr(w, "job not found", http.StatusNotFound)
		return
	}
	n, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || n < 0 || n >= len(j.Outputs) {
		jsonErr(w, fmt.Sprintf("output %s not found (job has %d)", r.PathValue("n"), len(j.Outputs)), http.StatusNotFound)
		return
	}
	path := a.Q.store.Abs(j.Outputs[n])
	if ct := mime.TypeByExtension(filepath.Ext(path)); ct != "" {
		w.Header().Set("Content-Type", ct)
	}
	http.ServeFile(w, r, path)
}

func (a *API) cancel(w http.ResponseWriter, r *http.Request) {
	proj, err := project(r)
	if err != nil {
		jsonErr(w, err.Error(), http.StatusBadRequest)
		return
	}
	j, err := a.Q.Cancel(r.Context(), r.PathValue("id"), proj)
	switch {
	case errors.Is(err, errNotFound):
		jsonErr(w, err.Error(), http.StatusNotFound)
	case errors.Is(err, errForbidden):
		jsonErr(w, err.Error(), http.StatusForbidden)
	case err != nil:
		jsonErr(w, err.Error(), http.StatusInternalServerError)
	default:
		writeJSON(w, http.StatusOK, a.view(r, j))
	}
}

// tag proxies to the WD14 tagger on the host, same request/response as :8097/tag.
func (a *API) tag(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxTagUpload)
	req, err := http.NewRequestWithContext(r.Context(), "POST", a.WD14+"/tag?"+r.URL.RawQuery, r.Body)
	if err != nil {
		jsonErr(w, err.Error(), http.StatusInternalServerError)
		return
	}
	req.Header.Set("Content-Type", r.Header.Get("Content-Type"))
	resp, err := a.HTTP.Do(req)
	if err != nil {
		jsonErr(w, "WD14 tagger unreachable: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body) //nolint:errcheck
}

func (a *API) queueStatus(w http.ResponseWriter, r *http.Request) {
	q := a.Q.store.ordered(a.Q.rank)
	a.Q.mu.Lock()
	running := a.Q.runID
	a.Q.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"window":     a.Q.gate.Check(r.Context()),
		"queued":     len(q),
		"running":    running != "",
		"by_project": a.Q.store.Counts(),
		"priority":   a.Q.cfg.Priority,
	})
}

func (a *API) listModels(w http.ResponseWriter, r *http.Request) {
	a.models.Lock()
	defer a.models.Unlock()
	if a.models.last != nil && time.Since(a.models.at) < 5*time.Minute {
		writeJSON(w, http.StatusOK, map[string]any{"models": a.models.last, "as_of": a.models.at})
		return
	}
	m, err := a.Q.comfy.Models(r.Context())
	if err != nil {
		if a.models.last != nil {
			// ComfyUI is down outside the window; the last list is still useful.
			writeJSON(w, http.StatusOK, map[string]any{"models": a.models.last, "as_of": a.models.at, "stale": true})
			return
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": "ComfyUI unreachable and no cached model list", "window": a.Q.gate.Check(r.Context())})
		return
	}
	a.models.last, a.models.at = m, time.Now()
	writeJSON(w, http.StatusOK, map[string]any{"models": m, "as_of": a.models.at})
}

func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v) //nolint:errcheck
}

func jsonErr(w http.ResponseWriter, msg string, code int) {
	writeJSON(w, code, map[string]string{"error": msg})
}
