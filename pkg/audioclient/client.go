// Package audioclient je Go klient pro AiStack services/audio — generování
// hudby a SFX lokálními modely.
//
// Klient záměrně nezná jména modelů. Volající popíše, co chce slyšet; který
// model to vyrobí, rozhoduje služba. To je celý smysl téhle vrstvy: až se
// ACE-Step vymění za něco jiného, tenhle balík ani jeho konzumenti se nezmění.
package audioclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client mluví s jednou instancí services/audio.
type Client struct {
	baseURL string
	apiKey  string
	http    *http.Client
	poll    time.Duration
}

// Option konfiguruje klienta.
type Option func(*Client)

// WithAPIKey nastaví klíč posílaný jako Bearer token.
func WithAPIKey(key string) Option { return func(c *Client) { c.apiKey = key } }

// WithHTTPClient podstrčí vlastní http.Client (testy, proxy, timeouty).
func WithHTTPClient(h *http.Client) Option { return func(c *Client) { c.http = h } }

// WithPollInterval mění, jak často se WaitJob ptá na stav.
func WithPollInterval(d time.Duration) Option { return func(c *Client) { c.poll = d } }

// New vytvoří klienta. baseURL je kořen služby, např. "http://spark:8093"
// nebo "https://llm.ol1n.com".
func New(baseURL string, opts ...Option) *Client {
	c := &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		// Generace běží asynchronně přes joby, takže tenhle timeout platí jen
		// na jednotlivý HTTP request, ne na celou generaci.
		http: &http.Client{Timeout: 120 * time.Second},
		poll: 2 * time.Second,
	}
	for _, opt := range opts {
		opt(c)
	}
	return c
}

// MusicSpec popisuje hudební stopu.
type MusicSpec struct {
	Prompt       string  `json:"prompt"`
	DurationSec  float64 `json:"duration_s"`
	Seed         *int64  `json:"seed,omitempty"`
	Lyrics       string  `json:"lyrics,omitempty"`
	Instrumental bool    `json:"instrumental"`
	BPM          int     `json:"bpm,omitempty"`
	Key          string  `json:"key,omitempty"`
	Loop         bool    `json:"loop"`
	Format       string  `json:"format,omitempty"`
	Variations   int     `json:"variations,omitempty"`
	Model        string  `json:"model,omitempty"`
}

// SFXSpec popisuje zvukový efekt.
type SFXSpec struct {
	Prompt      string  `json:"prompt"`
	DurationSec float64 `json:"duration_s"`
	Seed        *int64  `json:"seed,omitempty"`
	Variations  int     `json:"variations,omitempty"`
	Mono        bool    `json:"mono"`
	Format      string  `json:"format,omitempty"`
	Model       string  `json:"model,omitempty"`
}

// Output je jeden hotový soubor.
type Output struct {
	URL          string  `json:"url"`
	Filename     string  `json:"filename"`
	Duration     float64 `json:"duration"`
	LoudnessLUFS float64 `json:"loudness_lufs"`
	TruePeakDB   float64 `json:"true_peak_db"`
	Seed         int64   `json:"seed"`
	SHA256       string  `json:"sha256"`
	Bytes        int64   `json:"bytes"`
}

// Job je stav generace.
type Job struct {
	JobID         string   `json:"job_id"`
	Kind          string   `json:"kind"`
	Status        string   `json:"status"`
	Model         string   `json:"model"`
	QueuePosition *int     `json:"queue_position"`
	Error         string   `json:"error"`
	Outputs       []Output `json:"outputs"`
}

// Model je položka katalogu včetně licence — Steam build musí vědět,
// čím assety vznikly.
type Model struct {
	Name       string `json:"name"`
	Kind       string `json:"kind"`
	Backend    string `json:"backend"`
	License    string `json:"license"`
	LicenseURL string `json:"license_url"`
	Commercial bool   `json:"commercial"`
	Note       string `json:"note"`
	Upstream   string `json:"upstream"`
	Available  bool   `json:"available"`
	Loaded     bool   `json:"loaded"`
	Detail     string `json:"detail"`
}

// APIError je chyba vrácená službou.
type APIError struct {
	StatusCode int
	Body       string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("audio API %d: %s", e.StatusCode, e.Body)
}

// GenerateMusic zařadí hudební job do fronty a vrátí ho ve stavu queued.
func (c *Client) GenerateMusic(ctx context.Context, spec MusicSpec) (Job, error) {
	return c.submit(ctx, "/v1/audio/music", spec)
}

// GenerateSFX zařadí SFX job do fronty a vrátí ho ve stavu queued.
func (c *Client) GenerateSFX(ctx context.Context, spec SFXSpec) (Job, error) {
	return c.submit(ctx, "/v1/audio/sfx", spec)
}

func (c *Client) submit(ctx context.Context, path string, spec any) (Job, error) {
	body, err := json.Marshal(spec)
	if err != nil {
		return Job{}, fmt.Errorf("marshal: %w", err)
	}
	var accepted struct {
		JobID         string `json:"job_id"`
		QueuePosition int    `json:"queue_position"`
	}
	if err := c.do(ctx, http.MethodPost, path, bytes.NewReader(body), &accepted); err != nil {
		return Job{}, err
	}
	return Job{
		JobID:         accepted.JobID,
		Status:        "queued",
		QueuePosition: &accepted.QueuePosition,
	}, nil
}

// JobStatus se jednou zeptá na stav jobu.
func (c *Client) JobStatus(ctx context.Context, jobID string) (Job, error) {
	var job Job
	err := c.do(ctx, http.MethodGet, "/v1/audio/jobs/"+url.PathEscape(jobID), nil, &job)
	return job, err
}

// WaitJob čeká na dokončení. Vrací chybu, když job selže, když vyprší ctx,
// nebo když timeout > 0 uplyne.
func (c *Client) WaitJob(ctx context.Context, jobID string, timeout time.Duration) (Job, error) {
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	ticker := time.NewTicker(c.poll)
	defer ticker.Stop()
	for {
		job, err := c.JobStatus(ctx, jobID)
		if err != nil {
			// Přechodná chyba sítě nesmí shodit čekání na hotový job;
			// rozhoduje až ctx.
			if ctx.Err() != nil {
				return Job{}, fmt.Errorf("čekání na job %s: %w", jobID, ctx.Err())
			}
		} else {
			switch job.Status {
			case "done":
				return job, nil
			case "error":
				return job, fmt.Errorf("job %s selhal: %s", jobID, job.Error)
			}
		}
		select {
		case <-ctx.Done():
			return Job{}, fmt.Errorf("čekání na job %s: %w", jobID, ctx.Err())
		case <-ticker.C:
		}
	}
}

// Download stáhne jeden výstup do w.
func (c *Client) Download(ctx context.Context, out Output, w io.Writer) (int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+out.URL, nil)
	if err != nil {
		return 0, fmt.Errorf("create request: %w", err)
	}
	c.auth(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("GET %s: %w", out.URL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return 0, &APIError{StatusCode: resp.StatusCode, Body: string(snippet)}
	}
	return io.Copy(w, resp.Body)
}

// Models vrátí katalog i s licencemi a dostupností.
func (c *Client) Models(ctx context.Context) ([]Model, error) {
	var models []Model
	err := c.do(ctx, http.MethodGet, "/v1/audio/models", nil, &models)
	return models, err
}

// Health řekne, jestli služba odpovídá.
func (c *Client) Health(ctx context.Context) error {
	var payload map[string]any
	return c.do(ctx, http.MethodGet, "/health", nil, &payload)
}

func (c *Client) do(ctx context.Context, method, path string, body io.Reader, out any) error {
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	c.auth(req)

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read %s %s: %w", method, path, err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &APIError{StatusCode: resp.StatusCode, Body: string(data)}
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("decode %s %s: %w (tělo: %.200s)", method, path, err, data)
	}
	return nil
}

func (c *Client) auth(req *http.Request) {
	if c.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
	}
}
