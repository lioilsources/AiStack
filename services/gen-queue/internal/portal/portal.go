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
	"crypto/subtle"
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
	p.rp.ServeHTTP(w, r)
}
