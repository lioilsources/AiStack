package ugc

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"time"
)

// Comfy is a minimal ComfyUI API client: upload input, queue a graph, wait,
// download an output file. Everything the UGC pipeline needs, nothing more.
type Comfy struct {
	URL    string
	Client *http.Client
}

func NewComfy(url string) *Comfy {
	return &Comfy{URL: url, Client: &http.Client{Timeout: 60 * time.Second}}
}

// UploadImage puts PNG bytes into ComfyUI's input folder under name.
func (c *Comfy) UploadImage(ctx context.Context, name string, png []byte) error {
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, err := mw.CreateFormFile("image", name)
	if err != nil {
		return err
	}
	fw.Write(png)
	mw.WriteField("overwrite", "true")
	mw.Close()
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL+"/upload/image", &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", mw.FormDataContentType())
	resp, err := c.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("upload: HTTP %d: %s", resp.StatusCode, b)
	}
	return nil
}

// Run queues a graph and waits for completion. Returns the history outputs.
// A node_errors response fails loudly — ComfyUI would otherwise silently
// drop invalid output nodes and report success (learned the hard way with
// Trellis2UnWrapAndRasterizer's required bvh input).
func (c *Comfy) Run(ctx context.Context, graph map[string]any, timeout time.Duration) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{"prompt": graph})
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL+"/prompt", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.Client.Do(req)
	if err != nil {
		return nil, err
	}
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	var queued struct {
		PromptID   string          `json:"prompt_id"`
		NodeErrors json.RawMessage `json:"node_errors"`
		Error      json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(raw, &queued); err != nil {
		return nil, fmt.Errorf("queue response: %s", truncate(raw, 300))
	}
	if len(queued.NodeErrors) > 2 { // vic nez "{}"
		return nil, fmt.Errorf("node_errors: %s", truncate(queued.NodeErrors, 600))
	}
	if queued.PromptID == "" {
		return nil, fmt.Errorf("no prompt_id: %s", truncate(raw, 300))
	}

	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(5 * time.Second):
		}
		hist, err := c.history(ctx, queued.PromptID)
		if err != nil || hist == nil {
			continue
		}
		status, _ := hist["status"].(map[string]any)
		if status["status_str"] == "error" {
			return nil, fmt.Errorf("execution error: %s", execError(status))
		}
		if done, _ := status["completed"].(bool); done {
			outputs, _ := hist["outputs"].(map[string]any)
			return outputs, nil
		}
	}
	return nil, fmt.Errorf("timeout after %s", timeout)
}

func (c *Comfy) history(ctx context.Context, id string) (map[string]any, error) {
	req, _ := http.NewRequestWithContext(ctx, "GET", c.URL+"/history/"+id, nil)
	resp, err := c.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var all map[string]map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&all); err != nil {
		return nil, err
	}
	return all[id], nil
}

// View downloads one file from ComfyUI's output folder.
func (c *Comfy) View(ctx context.Context, filename, subfolder, kind string) ([]byte, error) {
	url := fmt.Sprintf("%s/view?filename=%s&subfolder=%s&type=%s", c.URL, filename, subfolder, kind)
	req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
	resp, err := c.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("view %s: HTTP %d", filename, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func execError(status map[string]any) string {
	msgs, _ := status["messages"].([]any)
	for _, m := range msgs {
		pair, _ := m.([]any)
		if len(pair) == 2 && pair[0] == "execution_error" {
			d, _ := json.Marshal(pair[1])
			return truncate(d, 400)
		}
	}
	return "unknown"
}

func truncate(b []byte, n int) string {
	if len(b) > n {
		return string(b[:n]) + "..."
	}
	return string(b)
}
