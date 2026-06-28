package backend

import (
	"context"
	"net/http"
	"time"
)

// NimSchnell calls the FLUX.1-Schnell NIM container synchronously.
// The container accepts {"prompt","width","height","steps","seed"}
// and returns {"artifacts":[{"base64":"..."}]}.
type NimSchnell struct {
	url     string
	timeout time.Duration
	client  *http.Client
}

func NewNimSchnell(url string, timeout time.Duration) *NimSchnell {
	return &NimSchnell{url: url, timeout: timeout, client: &http.Client{}}
}

func (n *NimSchnell) Infer(ctx context.Context, payload []byte) ([]byte, error) {
	return doInfer(ctx, n.client, n.url, n.timeout, payload)
}
