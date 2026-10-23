package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
)

type client struct {
	base, token string
	http        *http.Client
	poll        time.Duration
	log         io.Writer // průběh (stderr)
}

func newClient() *client {
	base := os.Getenv("SPARK_BROKER")
	if base == "" {
		base = "http://spark-99bb:8100"
	}
	return &client{base: base, token: os.Getenv("SPARK_BROKER_TOKEN"),
		http: &http.Client{Timeout: 30 * time.Second}, poll: 30 * time.Second, log: os.Stderr}
}

// apiError je odpověď brokera mimo 2xx ({"error": "...", "job": {...}}).
type apiError struct {
	Status int
	Msg    string
}

func (e *apiError) Error() string { return fmt.Sprintf("%s (HTTP %d)", e.Msg, e.Status) }

// raw pošle požadavek a vrátí tělo; chyba sítě nebo HTTP mimo 2xx je error.
func (c *client) raw(method, path string, body any) ([]byte, error) {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.base+path, rd)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("broker %s nedostupný: %w", c.base, err)
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode/100 != 2 {
		var e struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(out, &e) != nil || e.Error == "" {
			e.Error = string(bytes.TrimSpace(out))
		}
		return nil, &apiError{resp.StatusCode, e.Error}
	}
	return out, nil
}

func (c *client) do(method, path string, body, out any) error {
	b, err := c.raw(method, path, body)
	if err != nil || out == nil {
		return err
	}
	return json.Unmarshal(b, out)
}

// dump vypíše odpověď jako odsazený JSON.
func (c *client) dump(method, path string) error {
	b, err := c.raw(method, path, nil)
	if err != nil {
		return err
	}
	var buf bytes.Buffer
	if json.Indent(&buf, b, "", "  ") != nil {
		buf.Reset()
		buf.Write(b)
	}
	buf.WriteByte('\n')
	_, err = os.Stdout.Write(buf.Bytes())
	return err
}

// short provede změnu úlohy a vypíše jeden řádek se stavem.
func (c *client) short(method, path string, body any) error {
	var j broker.Job
	if err := c.do(method, path, body, &j); err != nil {
		return err
	}
	fmt.Println(jobLine(&j))
	return nil
}

func (c *client) submit(r broker.SubmitReq) (*broker.Job, error) {
	var j broker.Job
	if err := c.do("POST", "/v1/jobs", r, &j); err != nil {
		return nil, err
	}
	fmt.Fprintln(c.log, jobLine(&j))
	return &j, nil
}

func (c *client) submitAndWait(r broker.SubmitReq) error {
	j, err := c.submit(r)
	if err != nil {
		return err
	}
	fmt.Println(j.ID)
	return c.wait(j.ID)
}

// wait čeká, až úloha poběží; hlásí změny důvodu čekání. Konečný stav = chyba.
func (c *client) wait(id string) error {
	last := ""
	for {
		var j broker.Job
		if err := c.do("GET", "/v1/jobs/"+id, nil, &j); err != nil {
			return err
		}
		switch {
		case j.State == broker.Running:
			fmt.Fprintln(c.log, "running:", id)
			return nil
		case j.Final():
			return fmt.Errorf("úloha %s skončila: %s %s", id, j.State, j.Error)
		}
		if j.Waiting != last {
			fmt.Fprintln(c.log, "čeká:", j.Waiting)
			last = j.Waiting
		}
		time.Sleep(c.poll)
	}
}

func jobLine(j *broker.Job) string {
	s := fmt.Sprintf("úloha %s: %s, priorita %d", j.ID, j.State, j.Priority)
	if j.PendingPriority > 0 {
		s += fmt.Sprintf(" (chce %d, čeká na uživatele)", j.PendingPriority)
	}
	if j.LeaseUntil != nil && !j.Final() {
		s += ", do " + j.LeaseUntil.Local().Format("15:04")
	}
	if j.Error != "" {
		s += " — " + j.Error
	}
	return s
}
