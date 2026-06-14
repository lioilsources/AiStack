package health

import (
	"context"
	"fmt"
	"net/http"
	"time"
)

const (
	DefaultTimeout  = 300 * time.Second
	DefaultInterval = 5 * time.Second
)

// WaitHealthy polls url with GET until it returns 200 or ctx/timeout expires.
func WaitHealthy(ctx context.Context, url string, interval, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	client := &http.Client{Timeout: 10 * time.Second}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	var lastErr error
	for {
		select {
		case <-ctx.Done():
			if lastErr != nil {
				return fmt.Errorf("health timeout for %s: %w", url, lastErr)
			}
			return fmt.Errorf("health timeout for %s", url)
		case <-ticker.C:
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
			if err != nil {
				lastErr = err
				continue
			}
			resp, err := client.Do(req)
			if err != nil {
				lastErr = err
				continue
			}
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			lastErr = fmt.Errorf("status %d", resp.StatusCode)
		}
	}
}
