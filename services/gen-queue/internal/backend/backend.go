package backend

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

// ErrNonRetryable marks client-side errors (4xx) that must not be retried.
var ErrNonRetryable = errors.New("non-retryable")

// Backend performs a synchronous NIM inference and returns raw PNG bytes.
type Backend interface {
	Infer(ctx context.Context, payload []byte) ([]byte, error)
}

// doInfer posts payload to url with the given timeout and decodes the NIM
// artifacts response ({"artifacts":[{"base64":"..."}]}) into PNG bytes.
func doInfer(ctx context.Context, client *http.Client, url string, timeout time.Duration, payload []byte) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg := fmt.Sprintf("HTTP %d: %s", resp.StatusCode, truncate(string(body), 300))
		if resp.StatusCode >= 400 && resp.StatusCode < 500 {
			return nil, fmt.Errorf("%w: %s", ErrNonRetryable, msg)
		}
		return nil, fmt.Errorf("%s", msg)
	}

	return decodeArtifacts(body)
}

func decodeArtifacts(body []byte) ([]byte, error) {
	var nimResp struct {
		Artifacts []struct {
			Base64 string `json:"base64"`
		} `json:"artifacts"`
	}
	if err := json.Unmarshal(body, &nimResp); err != nil {
		return nil, fmt.Errorf("parse NIM response: %w", err)
	}
	if len(nimResp.Artifacts) == 0 || nimResp.Artifacts[0].Base64 == "" {
		return nil, fmt.Errorf("empty artifacts in NIM response")
	}
	png, err := base64.StdEncoding.DecodeString(nimResp.Artifacts[0].Base64)
	if err != nil {
		return nil, fmt.Errorf("decode base64: %w", err)
	}
	return png, nil
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}
