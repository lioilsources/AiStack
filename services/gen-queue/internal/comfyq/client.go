package comfyq

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"time"
)

// ErrInvalid marks a ComfyUI rejection of the workflow itself (HTTP 4xx,
// node_errors, execution error): retrying cannot help, the job errors out.
// Every other error (connection refused, 5xx, interrupted run) is transient:
// the job goes back to the queue.
var ErrInvalid = errors.New("invalid workflow")

// ErrInterrupted means the run was interrupted by someone else, typically the
// scheduler's safety /interrupt when switching away from comfy.
var ErrInterrupted = errors.New("interrupted")

type Client struct {
	URL  string
	HTTP *http.Client
}

func NewClient(u string) *Client {
	return &Client{URL: u, HTTP: &http.Client{Timeout: 60 * time.Second}}
}

func (c *Client) getJSON(ctx context.Context, path string, v any) error {
	req, err := http.NewRequestWithContext(ctx, "GET", c.URL+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: HTTP %d", path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(v)
}

func (c *Client) SystemStats(ctx context.Context) (systemStats, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var s systemStats
	return s, c.getJSON(ctx, "/system_stats", &s)
}

type queueState struct {
	Running []([]any) `json:"queue_running"`
	Pending []([]any) `json:"queue_pending"`
}

func (c *Client) queue(ctx context.Context) (queueState, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var q queueState
	return q, c.getJSON(ctx, "/queue", &q)
}

func (c *Client) QueueLen(ctx context.Context) (int, int, error) {
	q, err := c.queue(ctx)
	return len(q.Running), len(q.Pending), err
}

// RunningPrompt returns the prompt_id ComfyUI is executing right now, or "".
func (c *Client) RunningPrompt(ctx context.Context) (string, error) {
	q, err := c.queue(ctx)
	if err != nil || len(q.Running) == 0 || len(q.Running[0]) < 2 {
		return "", err
	}
	id, _ := q.Running[0][1].(string)
	return id, nil
}

func (c *Client) Interrupt(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL+"/interrupt", nil)
	if err != nil {
		return err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

func (c *Client) Upload(ctx context.Context, name string, data []byte) error {
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, err := mw.CreateFormFile("image", name)
	if err != nil {
		return err
	}
	fw.Write(data)                     //nolint:errcheck
	mw.WriteField("overwrite", "true") //nolint:errcheck
	mw.Close()
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL+"/upload/image", &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", mw.FormDataContentType())
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		err := fmt.Errorf("upload %s: HTTP %d: %s", name, resp.StatusCode, b)
		if resp.StatusCode < 500 {
			return fmt.Errorf("%w: %v", ErrInvalid, err)
		}
		return err
	}
	return nil
}

// Submit queues the graph and returns its prompt_id.
func (c *Client) Submit(ctx context.Context, workflow json.RawMessage, clientID string) (string, error) {
	body, _ := json.Marshal(map[string]any{"prompt": workflow, "client_id": clientID})
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL+"/prompt", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 500 {
		return "", fmt.Errorf("prompt: HTTP %d: %s", resp.StatusCode, clip(raw, 300))
	}
	var r struct {
		PromptID   string          `json:"prompt_id"`
		NodeErrors json.RawMessage `json:"node_errors"`
	}
	_ = json.Unmarshal(raw, &r)
	if resp.StatusCode >= 400 || len(r.NodeErrors) > 2 {
		return "", fmt.Errorf("%w: %s", ErrInvalid, clip(raw, 800))
	}
	if r.PromptID == "" {
		return "", fmt.Errorf("prompt: no prompt_id: %s", clip(raw, 300))
	}
	return r.PromptID, nil
}

type OutputFile struct {
	Filename  string `json:"filename"`
	Subfolder string `json:"subfolder"`
	Type      string `json:"type"`
}

// Poll returns (done, outputs, err). err wrapping ErrInvalid or
// ErrInterrupted ends the run; any other err is a failed poll.
func (c *Client) Poll(ctx context.Context, promptID string) (bool, []OutputFile, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	var all map[string]struct {
		Status struct {
			StatusStr string  `json:"status_str"`
			Completed bool    `json:"completed"`
			Messages  [][]any `json:"messages"`
		} `json:"status"`
		Outputs map[string]map[string]json.RawMessage `json:"outputs"`
	}
	if err := c.getJSON(ctx, "/history/"+url.PathEscape(promptID), &all); err != nil {
		return false, nil, err
	}
	h, ok := all[promptID]
	if !ok {
		return false, nil, nil
	}
	if h.Status.StatusStr == "error" {
		for _, m := range h.Status.Messages {
			if len(m) == 2 && m[0] == "execution_interrupted" {
				return false, nil, ErrInterrupted
			}
		}
		for _, m := range h.Status.Messages {
			if len(m) == 2 && m[0] == "execution_error" {
				d, _ := json.Marshal(m[1])
				return false, nil, fmt.Errorf("%w: %s", ErrInvalid, clip(d, 800))
			}
		}
		return false, nil, fmt.Errorf("%w: execution error", ErrInvalid)
	}
	if !h.Status.Completed {
		return false, nil, nil
	}
	return true, collectOutputs(h.Outputs), nil
}

// collectOutputs takes every saved file (type "output") from every node, in
// node-id order: images, gifs/videos (VHS) or anything else with a filename.
// Previews (type "temp") are skipped.
func collectOutputs(outputs map[string]map[string]json.RawMessage) []OutputFile {
	var ids []string
	for id := range outputs {
		ids = append(ids, id)
	}
	sortNodeIDs(ids)
	var out []OutputFile
	for _, id := range ids {
		var keys []string
		for k := range outputs[id] {
			keys = append(keys, k)
		}
		sortStrings(keys)
		for _, k := range keys {
			var files []OutputFile
			if json.Unmarshal(outputs[id][k], &files) != nil {
				continue
			}
			for _, f := range files {
				if f.Filename != "" && f.Type == "output" {
					out = append(out, f)
				}
			}
		}
	}
	return out
}

func (c *Client) View(ctx context.Context, f OutputFile) ([]byte, error) {
	q := url.Values{"filename": {f.Filename}, "subfolder": {f.Subfolder}, "type": {f.Type}}
	req, err := http.NewRequestWithContext(ctx, "GET", c.URL+"/view?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := (&http.Client{Timeout: 5 * time.Minute}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("view %s: HTTP %d", f.Filename, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 512<<20))
}

// Models lists checkpoints, LoRAs and VAEs from /object_info.
func (c *Client) Models(ctx context.Context) (map[string][]string, error) {
	nodes := map[string][2]string{
		"checkpoints": {"CheckpointLoaderSimple", "ckpt_name"},
		"loras":       {"LoraLoader", "lora_name"},
		"vae":         {"VAELoader", "vae_name"},
		"unet":        {"UNETLoader", "unet_name"},
	}
	out := map[string][]string{}
	for kind, n := range nodes {
		var info map[string]struct {
			Input struct {
				Required map[string][]json.RawMessage `json:"required"`
			} `json:"input"`
		}
		if err := c.getJSON(ctx, "/object_info/"+n[0], &info); err != nil {
			return nil, err
		}
		field := info[n[0]].Input.Required[n[1]]
		if len(field) == 0 {
			continue
		}
		var names []string
		if json.Unmarshal(field[0], &names) != nil {
			// newer ComfyUI: ["COMBO", {"options": [...]}]
			var opts struct {
				Options []string `json:"options"`
			}
			if len(field) > 1 {
				_ = json.Unmarshal(field[1], &opts)
			}
			names = opts.Options
		}
		out[kind] = names
	}
	return out, nil
}

func clip(b []byte, n int) string {
	if len(b) > n {
		return string(b[:n]) + "..."
	}
	return string(b)
}
