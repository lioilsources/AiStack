// Package portal je reverzní proxy na ComfyUI pro mobilní klienty (appka
// ComfyUI Portal), které umí jen "Authorization: Bearer <token>" a ne
// service token CF Access. comfyui.ol1n.com zůstává za CF Access; tahle
// proxy běží na vlastním hostname bez Access a pouští jen se správným tokenem.
//
// Token se bere z hlavičky Authorization (Bearer) nebo z ?token= (WebSocket
// klienti, které hlavičky neposílají — stejná konvence jako ComfyUI-Login).
// WebSocket /ws proxuje httputil.ReverseProxy sám (Upgrade).
package portal

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

type Proxy struct {
	token []byte
	rp    *httputil.ReverseProxy
}

// New vrací nil, když token chybí — proxy se pak nespouští vůbec.
func New(comfyURL, token string) (*Proxy, error) {
	if token == "" {
		return nil, nil
	}
	u, err := url.Parse(comfyURL)
	if err != nil {
		return nil, err
	}
	rp := httputil.NewSingleHostReverseProxy(u)
	base := rp.Director
	rp.Director = func(r *http.Request) {
		base(r)
		r.Host = u.Host
		// Token dál do ComfyUI nepouštět.
		r.Header.Del("Authorization")
		q := r.URL.Query()
		if q.Has("token") {
			q.Del("token")
			r.URL.RawQuery = q.Encode()
		}
		// ComfyUI kontroluje Origin proti Host (ochrana proti CSRF z prohlížeče);
		// nativní appka žádný neposílá, prohlížeč přes proxy by neprošel.
		r.Header.Del("Origin")
	}
	rp.FlushInterval = 100 * time.Millisecond
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		slog.Warn("portal: comfyui nedostupné", "path", r.URL.Path, "err", err)
		http.Error(w, "ComfyUI teď neběží (okno comfy je 7:00–13:00)", http.StatusServiceUnavailable)
	}
	return &Proxy{token: []byte(token), rp: rp}, nil
}

func (p *Proxy) authorized(r *http.Request) bool {
	got := r.URL.Query().Get("token")
	if h := r.Header.Get("Authorization"); h != "" {
		got = strings.TrimSpace(strings.TrimPrefix(h, "Bearer"))
	}
	return got != "" && subtle.ConstantTimeCompare([]byte(got), p.token) == 1
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !p.authorized(r) {
		w.Header().Set("WWW-Authenticate", `Bearer realm="comfyportal"`)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if r.URL.Path == "/cpe/workflow/save" && !p.safeSave(w, r) {
		return
	}
	p.rp.ServeHTTP(w, r)
}

// maxSaveBody omezuje tělo /cpe/workflow/save (workflow JSON má desítky kB).
const maxSaveBody = 16 << 20

// safeSave hlídá díru v comfy-portal-endpoint 1.2.1: /cpe/workflow/save
// skládá cestu os.path.join(workflow_dir, name) bez kontroly, takže "../x"
// nebo absolutní cesta zapíše .json kamkoli. Pouštíme jen holé jméno souboru.
// Tělo po přečtení vrací zpátky do requestu.
func (p *Proxy) safeSave(w http.ResponseWriter, r *http.Request) bool {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxSaveBody))
	r.Body.Close()
	if err != nil {
		http.Error(w, "tělo je moc velké", http.StatusRequestEntityTooLarge)
		return false
	}
	var req struct {
		Name *string `json:"name"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "neplatný JSON", http.StatusBadRequest)
		return false
	}
	if req.Name != nil && !safeName(*req.Name) {
		slog.Warn("portal: odmítnuto jméno workflow", "name", *req.Name)
		http.Error(w, "jméno workflow nesmí obsahovat cestu", http.StatusBadRequest)
		return false
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	return true
}

func safeName(n string) bool {
	return n != "" && !strings.ContainsAny(n, "/\\\x00") && !strings.HasPrefix(n, ".")
}
