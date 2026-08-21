package ugc

import (
	"bytes"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// NAS pushes finished items to ugc-api on JODA. When the NAS is down the
// bundle lands in the spool directory and a background loop retries — Spark
// holds no state beyond that (the NAS is the single source of truth).
type NAS struct {
	URL      string // http://192.168.88.88:8095
	SpoolDir string
	Client   *http.Client
}

func NewNAS(url, spool string) *NAS {
	os.MkdirAll(spool, 0o755)
	return &NAS{URL: url, SpoolDir: spool, Client: &http.Client{Timeout: 120 * time.Second}}
}

func (n *NAS) Push(id string, glb, preview, meta []byte) error {
	if err := n.post(id, glb, preview, meta); err != nil {
		n.spool(id, glb, preview, meta)
		return err
	}
	return nil
}

func (n *NAS) post(id string, glb, preview, meta []byte) error {
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, _ := mw.CreateFormFile("glb", id+".glb")
	fw.Write(glb)
	if len(preview) > 0 {
		pw, _ := mw.CreateFormFile("preview", id+".png")
		pw.Write(preview)
	}
	mw.WriteField("meta", string(meta))
	mw.Close()

	req, err := http.NewRequest("POST", n.URL+"/jobs", &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", mw.FormDataContentType())
	resp, err := n.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("NAS HTTP %d: %s", resp.StatusCode, b)
	}
	return nil
}

func (n *NAS) spool(id string, glb, preview, meta []byte) {
	dir := filepath.Join(n.SpoolDir, id)
	os.MkdirAll(dir, 0o755)
	os.WriteFile(filepath.Join(dir, "model.glb"), glb, 0o644)
	if len(preview) > 0 {
		os.WriteFile(filepath.Join(dir, "preview.png"), preview, 0o644)
	}
	os.WriteFile(filepath.Join(dir, "meta.json"), meta, 0o644)
}

// RetrySpool re-pushes spooled bundles every 5 minutes until they land.
func (n *NAS) RetrySpool() {
	for {
		time.Sleep(5 * time.Minute)
		entries, err := os.ReadDir(n.SpoolDir)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			id := e.Name()
			dir := filepath.Join(n.SpoolDir, id)
			glb, err1 := os.ReadFile(filepath.Join(dir, "model.glb"))
			meta, err2 := os.ReadFile(filepath.Join(dir, "meta.json"))
			if err1 != nil || err2 != nil {
				continue
			}
			preview, _ := os.ReadFile(filepath.Join(dir, "preview.png"))
			if err := n.post(id, glb, preview, meta); err != nil {
				slog.Warn("spool retry failed", "id", id, "err", err)
				continue
			}
			os.RemoveAll(dir)
			slog.Info("spooled job delivered", "id", id)
		}
	}
}
