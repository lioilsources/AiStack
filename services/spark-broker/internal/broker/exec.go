package broker

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Executor je všechno, čím broker sahá na stroj. Testy ho nahrazují falešným.
type Executor interface {
	// Shell spustí provisioning příkaz (bash -lc) a vrátí výstup.
	Shell(ctx context.Context, cmd string) (string, error)
	// Start spustí run stage; výstup jde do logPath.
	Start(cmd, dir, logPath string, env []string) (Proc, error)
	// Probe přečte stav stroje.
	Probe(ctx context.Context) Probe
}

type Proc interface {
	// Wait vrátí exit kód a metriky z posledního JSON řádku výstupu.
	Wait() (code int, metrics map[string]Count)
	Terminate()
	Kill()
}

type Probe struct {
	MemAvailGiB int   `json:"mem_available_gib"`
	GPUTempC    int   `json:"gpu_temp_c"`
	GPUUtil     int   `json:"gpu_util"`
	ComfyActive int64 `json:"comfy_activity"` // roste s každým promptem v ComfyUI (nebo -1)
}

// Host je skutečný SPARK.
type Host struct {
	ComfyURL string
}

func (h *Host) Shell(ctx context.Context, cmd string) (string, error) {
	c := exec.CommandContext(ctx, "bash", "-lc", cmd)
	c.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	c.Cancel = func() error { return syscall.Kill(-c.Process.Pid, syscall.SIGTERM) }
	c.WaitDelay = 30 * time.Second
	out, err := c.CombinedOutput()
	return string(out), err
}

type hostProc struct {
	cmd  *exec.Cmd
	log  string
	done chan struct{}
}

func (h *Host) Start(cmd, dir, logPath string, env []string) (Proc, error) {
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	c := exec.Command("bash", "-lc", cmd)
	c.Dir = expandHome(dir)
	c.Env = append(os.Environ(), env...)
	c.Stdout, c.Stderr = f, f
	c.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := c.Start(); err != nil {
		f.Close()
		return nil, err
	}
	p := &hostProc{cmd: c, log: logPath, done: make(chan struct{})}
	go func() { _ = c.Wait(); f.Close(); close(p.done) }()
	return p, nil
}

func (p *hostProc) Wait() (int, map[string]Count) {
	<-p.done
	code := p.cmd.ProcessState.ExitCode()
	if ws, ok := p.cmd.ProcessState.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		code = 128 + int(ws.Signal())
	}
	return code, lastMetrics(p.log)
}

func (p *hostProc) Terminate() { _ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGTERM) }
func (p *hostProc) Kill()      { _ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL) }

// lastMetrics najde poslední řádek logu, který je JSON objekt s počty
// ({"images":{"ok":44,"failed":2}}) — dohoda s run úlohami.
func lastMetrics(path string) map[string]Count {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	if st, err := f.Stat(); err == nil && st.Size() > 256<<10 {
		_, _ = f.Seek(-256<<10, io.SeekEnd)
	}
	var found map[string]Count
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if !strings.HasPrefix(line, "{") {
			continue
		}
		var m map[string]Count
		if json.Unmarshal([]byte(line), &m) == nil && len(m) > 0 {
			found = m
		}
	}
	return found
}

func (h *Host) Probe(ctx context.Context) Probe {
	p := Probe{MemAvailGiB: -1, GPUTempC: -1, GPUUtil: -1, ComfyActive: -1}
	if b, err := os.ReadFile("/proc/meminfo"); err == nil {
		for _, l := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(l, "MemAvailable:") {
				f := strings.Fields(l)
				if kb, err := strconv.Atoi(f[1]); err == nil {
					p.MemAvailGiB = kb / 1048576
				}
			}
		}
	}
	cctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if out, err := exec.CommandContext(cctx, "nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
		"--format=csv,noheader,nounits").Output(); err == nil {
		f := strings.Split(strings.TrimSpace(string(out)), ",")
		if len(f) == 2 {
			p.GPUTempC, _ = strconv.Atoi(strings.TrimSpace(f[0]))
			p.GPUUtil, _ = strconv.Atoi(strings.TrimSpace(f[1]))
		}
	}
	if h.ComfyURL != "" {
		p.ComfyActive = comfyActivity(cctx, h.ComfyURL)
	}
	return p
}

// comfyActivity: počet promptů v historii + běžící/čekající. Mění se, když
// v ComfyUI někdo pracuje; -1 když ComfyUI neodpovídá.
func comfyActivity(ctx context.Context, base string) int64 {
	get := func(path string, v any) error {
		req, _ := http.NewRequestWithContext(ctx, "GET", base+path, nil)
		r, err := http.DefaultClient.Do(req)
		if err != nil {
			return err
		}
		defer r.Body.Close()
		if r.StatusCode != 200 {
			return fmt.Errorf("%d", r.StatusCode)
		}
		return json.NewDecoder(r.Body).Decode(v)
	}
	var q struct {
		Running []any `json:"queue_running"`
		Pending []any `json:"queue_pending"`
	}
	if get("/queue", &q) != nil {
		return -1
	}
	var hist map[string]any
	if get("/history?max_items=10000", &hist) != nil {
		return -1
	}
	// běžící úloha = aktivita pokaždé, proto čas, ne jen počet
	n := int64(len(hist))
	if len(q.Running)+len(q.Pending) > 0 {
		n += time.Now().Unix()
	}
	return n
}

func expandHome(p string) string {
	if strings.HasPrefix(p, "~/") {
		if h, err := os.UserHomeDir(); err == nil {
			return h + p[1:]
		}
	}
	return p
}
