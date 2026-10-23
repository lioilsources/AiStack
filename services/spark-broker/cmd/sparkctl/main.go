// sparkctl — klient spark-brokera pro Claude sessions, skripty a lidi.
//
// Rozhraní je stejné jako u původního bash skriptu (skripty na Macu ho volají):
// id úlohy jde na stdout, průběh a chyby na stderr, chyba API = exit 1.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/ol1n/AiStack/spark-broker/internal/broker"
)

const usage = `sparkctl — fronta úloh SPARKu (spark-broker)

Přehled
  sparkctl ls [projekt]                       co běží, fronta, priority, kolik zbývá, proč čeká
  sparkctl get <id>                           detail úlohy (JSON)
  sparkctl jobs [projekt]                     všechny úlohy i hotové (JSON)
  sparkctl status | catalog                   surový stav · profily, služby, stropy priorit (JSON)

Okno uživatele (priorita 100, jen s doslovnými slovy uživatele)
  sparkctl window <služby> <délka> "<slova uživatele>"
      např. sparkctl window comfyui 3h "chci comfy na 3 hodiny"

Úlohy
  sparkctl lease <projekt> <služby> <min> [priorita] [název]   rezervace; vypíše id, čeká na running
  sparkctl run <projekt> <služby> <priorita> "<příkaz na SPARKu>" [název]   dávku spustí broker
  sparkctl wait <id>                          čeká na running
  sparkctl hb <id> [metriky-json]             heartbeat lease (aspoň každých 5 min)
  sparkctl done <id> [metriky-json]           konec, např. '{"images":{"ok":44,"failed":2}}'
  sparkctl fail <id> "<chyba>"                konec chybou (hned na Telegram)
  sparkctl extend <id> [min]                  prodloužit lease (výchozí 30)
  sparkctl prio <id> <priorita>               nad strop projektu = dotaz na uživatele
  sparkctl approve <id>                       schválit prioritu nad strop — JEN na pokyn uživatele
  sparkctl cancel <id>
  sparkctl pause | resume                     nic nového nestartovat · zase jet

Služby víc najednou oddělit čárkou: comfyui,audio-music. Délka: 3h, 90m nebo minuty.
Prostředí: SPARK_BROKER (výchozí http://spark-99bb:8100), SPARK_BROKER_TOKEN, SPARK_SESSION.
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		var u usageErr
		if errors.As(err, &u) {
			fmt.Fprintln(os.Stderr, "použití: sparkctl", string(u))
			os.Exit(2)
		}
		fmt.Fprintln(os.Stderr, "sparkctl:", err)
		os.Exit(1)
	}
}

type usageErr string

func (u usageErr) Error() string { return string(u) }

func run(args []string) error {
	c := newClient()
	cmd := "status"
	if len(args) > 0 {
		cmd, args = args[0], args[1:]
	}
	arg := func(i int, def string) string {
		if i < len(args) {
			return args[i]
		}
		return def
	}
	need := func(n int, u string) error {
		if len(args) < n {
			return usageErr(u)
		}
		return nil
	}

	switch cmd {
	case "status":
		return c.dump("GET", "/v1/status")
	case "catalog":
		return c.dump("GET", "/v1/catalog")
	case "jobs":
		return c.dump("GET", jobsPath(arg(0, "")))
	case "get":
		if err := need(1, "get <id>"); err != nil {
			return err
		}
		return c.dump("GET", "/v1/jobs/"+args[0])
	case "ls", "list":
		var st broker.Status
		var jobs []*broker.Job
		if err := c.do("GET", "/v1/status", nil, &st); err != nil {
			return err
		}
		if err := c.do("GET", jobsPath(arg(0, "")), nil, &jobs); err != nil {
			return err
		}
		overview(os.Stdout, st, jobs, time.Now())
		return nil

	case "window":
		if err := need(3, `window <služby> <délka> "<doslovná slova uživatele>"`); err != nil {
			return err
		}
		min, err := minutes(args[1])
		if err != nil {
			return err
		}
		return c.submitAndWait(broker.SubmitReq{
			Project: "user", Session: session(), Title: "okno " + args[0],
			OnBehalfOfUser: true, UserQuote: args[2],
			Stages: []broker.Stage{{Services: services(args[0]), Mode: broker.ModeLease, LeaseMin: min}},
		})
	case "lease":
		if err := need(3, "lease <projekt> <služby> <min> [priorita] [název]"); err != nil {
			return err
		}
		min, err := minutes(args[2])
		if err != nil {
			return err
		}
		prio, err := strconv.Atoi(arg(3, "20"))
		if err != nil {
			return fmt.Errorf("priorita %q není číslo", arg(3, ""))
		}
		return c.submitAndWait(broker.SubmitReq{
			Project: args[0], Session: session(), Title: arg(4, args[1]), Priority: prio,
			Stages: []broker.Stage{{Services: services(args[1]), Mode: broker.ModeLease, LeaseMin: min, EstimateMin: min}},
		})
	case "run":
		if err := need(4, `run <projekt> <služby> <priorita> "<příkaz>" [název]`); err != nil {
			return err
		}
		prio, err := strconv.Atoi(args[2])
		if err != nil {
			return fmt.Errorf("priorita %q není číslo", args[2])
		}
		j, err := c.submit(broker.SubmitReq{
			Project: args[0], Session: session(), Title: arg(4, args[3]), Priority: prio, Resume: true,
			Stages: []broker.Stage{{Services: services(args[1]), Mode: broker.ModeRun, Command: args[3]}},
		})
		if err != nil {
			return err
		}
		fmt.Println(j.ID)
		return nil
	case "wait":
		if err := need(1, "wait <id>"); err != nil {
			return err
		}
		return c.wait(args[0])

	case "hb":
		if err := need(1, "hb <id> [metriky-json]"); err != nil {
			return err
		}
		m, err := metrics(arg(1, ""))
		if err != nil {
			return err
		}
		return c.do("POST", "/v1/jobs/"+args[0]+"/heartbeat", map[string]any{"metrics": m}, nil)
	case "done":
		if err := need(1, "done <id> [metriky-json]"); err != nil {
			return err
		}
		m, err := metrics(arg(1, ""))
		if err != nil {
			return err
		}
		return c.short("POST", "/v1/jobs/"+args[0]+"/done", broker.DoneReq{Metrics: m})
	case "fail":
		if err := need(2, `fail <id> "<chyba>"`); err != nil {
			return err
		}
		return c.short("POST", "/v1/jobs/"+args[0]+"/done", broker.DoneReq{Error: args[1]})
	case "extend":
		if err := need(1, "extend <id> [min]"); err != nil {
			return err
		}
		min, err := minutes(arg(1, "30"))
		if err != nil {
			return err
		}
		return c.short("POST", fmt.Sprintf("/v1/jobs/%s/extend?minutes=%d", args[0], min), nil)
	case "prio":
		if err := need(2, "prio <id> <priorita>"); err != nil {
			return err
		}
		p, err := strconv.Atoi(args[1])
		if err != nil {
			return fmt.Errorf("priorita %q není číslo", args[1])
		}
		return c.short("PATCH", "/v1/jobs/"+args[0], broker.PatchReq{Priority: &p})
	case "approve":
		if err := need(1, "approve <id>"); err != nil {
			return err
		}
		return c.short("POST", "/v1/jobs/"+args[0]+"/approve", nil)
	case "cancel":
		if err := need(1, "cancel <id>"); err != nil {
			return err
		}
		return c.short("DELETE", "/v1/jobs/"+args[0], nil)
	case "pause", "resume":
		var st broker.Status
		if err := c.do("POST", "/v1/"+cmd, nil, &st); err != nil {
			return err
		}
		fmt.Printf("broker %s\n", map[bool]string{true: "pozastaven", false: "jede"}[st.Paused])
		return nil

	case "help", "-h", "--help":
		fmt.Print(usage)
		return nil
	default:
		fmt.Fprint(os.Stderr, usage)
		return usageErr(cmd + ": neznámý příkaz")
	}
}

func jobsPath(project string) string {
	p := "/v1/jobs?all=1"
	if project != "" {
		p += "&project=" + project
	}
	return p
}

func session() string {
	for _, k := range []string{"SPARK_SESSION", "CLAUDE_SESSION_NAME"} {
		if v := os.Getenv(k); v != "" {
			return v
		}
	}
	wd, _ := os.Getwd()
	return filepath.Base(wd)
}

func services(s string) []string {
	var out []string
	for _, x := range strings.Split(s, ",") {
		if x = strings.TrimSpace(x); x != "" {
			out = append(out, x)
		}
	}
	return out
}

// minutes: 3h, 90m, 90.
func minutes(s string) (int, error) {
	mul, num := 1, s
	switch {
	case strings.HasSuffix(s, "h"):
		mul, num = 60, strings.TrimSuffix(s, "h")
	case strings.HasSuffix(s, "m"):
		num = strings.TrimSuffix(s, "m")
	}
	n, err := strconv.Atoi(num)
	if err != nil || n <= 0 {
		return 0, fmt.Errorf("délka %q: čekám např. 3h, 90m nebo 90", s)
	}
	return n * mul, nil
}

func metrics(s string) (map[string]broker.Count, error) {
	if strings.TrimSpace(s) == "" {
		return nil, nil
	}
	var m map[string]broker.Count
	if err := json.Unmarshal([]byte(s), &m); err != nil {
		return nil, fmt.Errorf(`metriky %q: čekám např. {"images":{"ok":44,"failed":2}}`, s)
	}
	return m, nil
}
