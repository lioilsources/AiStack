package main

// hold a exec nahrazují bash obaly z ~/ops-director (broker-lib.sh, hold.sh, chain.sh):
// heartbeat, čekání po odsunutí, uvolnění po doběhnutí lokálního procesu.

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
)

// opts jsou volby --x hodnota, které smí stát kdekoli před `--`.
type opts struct {
	after  []string
	at     *time.Time // --at: jednorázová akce na čas
	proc   string     // hold: uvolnit, až tenhle lokální proces (pgrep -f) běžel a skončil
	idFile string     // zapsat id, jakmile úloha běží (pro jinou session, která na ni čeká)
	dir    string     // exec: pracovní adresář příkazu
	cmd    []string
}

func parseOpts(args []string) ([]string, opts, error) {
	var o opts
	var rest []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			o.cmd = args[i+1:]
			break
		}
		var dst *string
		switch a {
		case "--after", "--at":
		case "--proc":
			dst = &o.proc
		case "--id-file":
			dst = &o.idFile
		case "--dir":
			dst = &o.dir
		default:
			rest = append(rest, a)
			continue
		}
		if i+1 >= len(args) {
			return nil, o, usageErr(a + " chce hodnotu")
		}
		i++
		switch {
		case dst != nil:
			*dst = args[i]
		case a == "--at":
			t, err := parseAt(args[i], time.Now())
			if err != nil {
				return nil, o, err
			}
			o.at = &t
		default:
			o.after = append(o.after, services(args[i])...)
		}
	}
	return rest, o, nil
}

// parseAt: "19:05" (dnes, už-li minulo, tak zítra), "2026-10-27 06:30", RFC3339,
// nebo za dobu "+90m" / "+2h".
func parseAt(v string, now time.Time) (time.Time, error) {
	if d, ok := strings.CutPrefix(v, "+"); ok {
		m, err := minutes(d)
		if err != nil {
			return time.Time{}, err
		}
		return now.Add(time.Duration(m) * time.Minute), nil
	}
	if t, err := time.ParseInLocation("15:04", v, now.Location()); err == nil {
		at := time.Date(now.Year(), now.Month(), now.Day(), t.Hour(), t.Minute(), 0, 0, now.Location())
		if !at.After(now) {
			at = at.AddDate(0, 0, 1)
		}
		return at, nil
	}
	if t, err := time.ParseInLocation("2006-01-02 15:04", v, now.Location()); err == nil {
		return t, nil
	}
	if t, err := time.Parse(time.RFC3339, v); err == nil {
		return t, nil
	}
	return time.Time{}, usageErr(fmt.Sprintf("--at %q: čekám 19:05, \"2026-10-27 06:30\" nebo +90m", v))
}

// procRunning: běží na tomhle stroji proces, jehož příkazová řádka obsahuje vzor?
var procRunning = func(pattern string) bool {
	return exec.Command("pgrep", "-f", pattern).Run() == nil
}

const beat = time.Minute

// hold drží lease heartbeatem: počká na running, po odsunutí znovu čeká, skončí s úlohou.
// S o.proc úlohu uvolní, až proces běžel a skončil.
func (c *client) hold(id string, o opts) error {
	if err := c.waitAndMark(id, o.idFile); err != nil {
		return err
	}
	seen := false
	for {
		time.Sleep(c.beat())
		var j broker.Job
		if err := c.do("GET", "/v1/jobs/"+id, nil, &j); err != nil {
			return err
		}
		switch {
		case j.Final():
			fmt.Fprintf(c.log, "%s skončila: %s %s\n", id, j.State, j.Error)
			return nil
		case j.State != broker.Running:
			fmt.Fprintf(c.log, "%s odsunuta (%s), čekám\n", id, j.State)
			if err := c.wait(id); err != nil {
				return err
			}
			continue
		}
		if err := c.do("POST", "/v1/jobs/"+id+"/heartbeat", map[string]any{"metrics": nil}, nil); err != nil {
			return err
		}
		if o.proc == "" {
			continue
		}
		if procRunning(o.proc) {
			seen = true
		} else if seen {
			fmt.Fprintf(c.log, "%s doběhl, uvolňuji %s\n", o.proc, id)
			return c.do("POST", "/v1/jobs/"+id+"/done", broker.DoneReq{}, nil)
		}
	}
}

func (c *client) waitAndMark(id, idFile string) error {
	if err := c.wait(id); err != nil {
		return err
	}
	if idFile != "" {
		return os.WriteFile(idFile, []byte(id+"\n"), 0o644)
	}
	return nil
}

func (c *client) beat() time.Duration {
	if c.poll < beat {
		return c.poll
	}
	return beat
}

// execJob: lease → příkaz s heartbeatem. Odsunutí příkaz ukončí (SIGTERM skupině) a po
// návratu do běhu ho pustí znovu, takže příkaz musí umět pokračovat. Lease se sám prodlužuje,
// dokud příkaz běží. Poslední řádek stdoutu ve tvaru {"images":{"ok":1,"failed":0}} jde do metrik.
func (c *client) execJob(r broker.SubmitReq, o opts) error {
	if len(o.cmd) == 0 {
		return usageErr("exec … -- <příkaz> [argumenty]")
	}
	j, err := c.submit(r)
	if err != nil {
		return err
	}
	fmt.Println(j.ID)
	if err := c.waitAndMark(j.ID, o.idFile); err != nil {
		return err
	}
	for {
		code, m, preempted, err := c.runOnce(j.ID, o)
		if err != nil {
			return err
		}
		if preempted {
			fmt.Fprintf(c.log, "%s odsunuta, příkaz zastaven; čekám ve frontě\n", j.ID)
			if err := c.wait(j.ID); err != nil {
				return err
			}
			continue
		}
		if code == 0 {
			return c.short("POST", "/v1/jobs/"+j.ID+"/done", broker.DoneReq{Metrics: m})
		}
		c.short("POST", "/v1/jobs/"+j.ID+"/done", broker.DoneReq{Error: fmt.Sprintf("%s skončil s kódem %d", o.cmd[0], code)})
		return fmt.Errorf("příkaz skončil s kódem %d", code)
	}
}

func (c *client) runOnce(id string, o opts) (code int, m map[string]broker.Count, preempted bool, err error) {
	cmd := exec.Command(o.cmd[0], o.cmd[1:]...)
	cmd.Dir = o.dir
	cmd.Stdin, cmd.Stderr = nil, os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	out, err := cmd.StdoutPipe()
	if err != nil {
		return 0, nil, false, err
	}
	if err := cmd.Start(); err != nil {
		return 0, nil, false, err
	}
	type result struct {
		err  error
		last string
	}
	exited := make(chan result, 1)
	go func() { last := tee(out, os.Stdout); exited <- result{cmd.Wait(), last} }() // Wait až po dočtení roury

	tick := time.NewTicker(c.beat())
	defer tick.Stop()
	for {
		select {
		case res := <-exited:
			var ee *exec.ExitError
			if errors.As(res.err, &ee) {
				code = ee.ExitCode()
			} else if res.err != nil {
				return 0, nil, false, res.err
			}
			m, _ = metrics(res.last)
			return code, m, false, nil
		case <-tick.C:
			var j broker.Job
			if err := c.do("GET", "/v1/jobs/"+id, nil, &j); err != nil {
				fmt.Fprintln(c.log, "broker:", err) // výpadek sítě příkaz nezabíjí
				continue
			}
			if j.State != broker.Running {
				syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
				<-exited
				if j.Final() {
					return 0, nil, false, fmt.Errorf("úloha %s skončila: %s %s", id, j.State, j.Error)
				}
				return 0, nil, true, nil
			}
			c.do("POST", "/v1/jobs/"+id+"/heartbeat", map[string]any{"metrics": nil}, nil)
			if j.LeaseUntil != nil && time.Until(*j.LeaseUntil) < 15*time.Minute {
				c.do("POST", fmt.Sprintf("/v1/jobs/%s/extend?minutes=30", id), nil, nil)
			}
		}
	}
}

// tee kopíruje řádky do w a vrátí poslední neprázdný.
func tee(r io.Reader, w io.Writer) string {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 1<<20)
	last := ""
	for sc.Scan() {
		line := sc.Text()
		fmt.Fprintln(w, line)
		if t := strings.TrimSpace(line); t != "" {
			last = t
		}
	}
	if !json.Valid([]byte(last)) {
		return ""
	}
	return last
}
