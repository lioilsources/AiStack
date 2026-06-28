package backend

import (
	"context"
	"net/http"
	"time"
)

// NimKontext calls the FLUX.1-Kontext NIM container synchronously.
// The container accepts {"prompt","image","cfg_scale","steps","seed","aspect_ratio"}
// and returns {"artifacts":[{"base64":"..."}]}.
type NimKontext struct {
	url     string
	timeout time.Duration
	client  *http.Client
}

func NewNimKontext(url string, timeout time.Duration) *NimKontext {
	return &NimKontext{url: url, timeout: timeout, client: &http.Client{}}
}

func (n *NimKontext) Infer(ctx context.Context, payload []byte) ([]byte, error) {
	return doInfer(ctx, n.client, n.url, n.timeout, payload)
}
