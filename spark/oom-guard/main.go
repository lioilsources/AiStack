// spark-oom-guard — zastaví GPU kontejner dřív, než nedostatek paměti zamrazí SPARK.
//
// Proč ne kernelový OOM killer ani earlyoom: na GB10 je paměť unifikovaná a CUDA
// alokace nepatří do RSS procesu ani do memory.current jeho cgroup (ověřeno
// 2026-09-26: flux-schnell 17 GiB na GPU, v cgroup 1,7 GiB anon). Při pádu
// 25. 9. 2026 bylo v anon+file LRU ~0,5 GiB, zbytek ze 121 GiB držel driver —
// OOM killer dvě hodiny zabíjel procesy s 1 MB RSS a nic neuvolnil. Oba
// nástroje vybírají oběť podle RSS, takže by střílely vedle.
//
// Co tedy funguje: MemAvailable pokles od driveru vidí a PSI vidí, že procesy
// stojí na alokaci. Guard hlídá obojí a zabíjí z pevného seznamu, který
// odpovídá tomu, kdo na SPARKu reálně drží GPU paměť — nejdřív ten největší.
//
// Kontejner se zabíjí přes Docker API (SIGKILL), protože docker to eviduje jako
// ruční stop a `restart: unless-stopped` ho nenahodí zpátky. Když daemon pod
// tlakem neodpoví, sáhne guard rovnou na cgroup.kill — restart policy pak
// sice může kontejner vrátit, ale paměť se uvolní hned.
//
// Když není co zabít a tlak trvá, guard stroj restartuje (sysrq s-u-b) — pořád
// lepší než hodiny v zatuhlém stavu a cesta ke SPARKu kvůli vypínači.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type config struct {
	targets      []string // "docker:<name>" nebo "cgroup:<absolutní cesta>"
	minAvailMiB  int64
	availFor     time.Duration
	psiFull      float64
	psiFullFor   time.Duration
	psiSome      float64
	psiSomeFor   time.Duration
	psiMaxAvail  int64 // PSI se počítá jen pod touhle MemAvailable (MiB)
	cooldown     time.Duration
	rebootAfter  time.Duration
	reboot       bool
	rebootStamp  string
	rebootMinGap time.Duration
	notify       string
	dryRun       bool
	interval     time.Duration
	heartbeat    time.Duration
}

func env(k, def string) string {
	if v, ok := os.LookupEnv(k); ok && v != "" {
		return v
	}
	return def
}

func envDur(k, def string) time.Duration {
	d, err := time.ParseDuration(env(k, def))
	if err != nil {
		log.Fatalf("%s: %v", k, err)
	}
	return d
}

func envFloat(k, def string) float64 {
	f, err := strconv.ParseFloat(env(k, def), 64)
	if err != nil {
		log.Fatalf("%s: %v", k, err)
	}
	return f
}

func envInt(k, def string) int64 {
	v, err := strconv.ParseInt(env(k, def), 10, 64)
	if err != nil {
		log.Fatalf("%s: %v", k, err)
	}
	return v
}

func loadConfig() config {
	return config{
		targets:      strings.Fields(env("GUARD_TARGETS", "docker:swarm-director")),
		minAvailMiB:  envInt("GUARD_MIN_AVAIL_MIB", "1024"),
		availFor:     envDur("GUARD_AVAIL_FOR", "3s"),
		psiFull:      envFloat("GUARD_PSI_FULL", "15"),
		psiFullFor:   envDur("GUARD_PSI_FULL_FOR", "10s"),
		psiSome:      envFloat("GUARD_PSI_SOME", "40"),
		psiSomeFor:   envDur("GUARD_PSI_SOME_FOR", "15s"),
		psiMaxAvail:  envInt("GUARD_PSI_MAX_AVAIL_MIB", "8192"),
		cooldown:     envDur("GUARD_COOLDOWN", "15s"),
		rebootAfter:  envDur("GUARD_REBOOT_AFTER", "3m"),
		reboot:       env("GUARD_REBOOT", "1") == "1",
		rebootStamp:  env("GUARD_REBOOT_STAMP", "/var/lib/spark-oom-guard/last-reboot"),
		rebootMinGap: envDur("GUARD_REBOOT_MIN_GAP", "1h"),
		notify:       env("GUARD_NOTIFY", ""),
		dryRun:       env("GUARD_DRY_RUN", "0") == "1",
		interval:     envDur("GUARD_INTERVAL", "1s"),
		heartbeat:    envDur("GUARD_HEARTBEAT", "1m"),
	}
}

// ── měření ───────────────────────────────────────────────────────────────────

type sample struct {
	availMiB int64
	someAvg  float64 // /proc/pressure/memory some avg10
	fullAvg  float64 // /proc/pressure/memory full avg10
}

func readSample() (sample, error) {
	var s sample
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return s, err
	}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if kb, ok := strings.CutPrefix(sc.Text(), "MemAvailable:"); ok {
			v, _ := strconv.ParseInt(strings.Fields(kb)[0], 10, 64)
			s.availMiB = v / 1024
			break
		}
	}
	f.Close()

	b, err := os.ReadFile("/proc/pressure/memory")
	if err != nil {
		return s, err
	}
	for _, line := range strings.Split(string(b), "\n") {
		fs := strings.Fields(line)
		if len(fs) < 2 {
			continue
		}
		v, _ := strconv.ParseFloat(strings.TrimPrefix(fs[1], "avg10="), 64)
		switch fs[0] {
		case "some":
			s.someAvg = v
		case "full":
			s.fullAvg = v
		}
	}
	return s, nil
}

// held počítá, jak dlouho podmínka nepřetržitě platí.
type held struct{ since time.Time }

func (h *held) update(now time.Time, cond bool, need time.Duration) bool {
	if !cond {
		h.since = time.Time{}
		return false
	}
	if h.since.IsZero() {
		h.since = now
	}
	return now.Sub(h.since) >= need
}

// ── oběti ────────────────────────────────────────────────────────────────────

var docker = &http.Client{
	Timeout: 5 * time.Second,
	Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", "/var/run/docker.sock")
		},
	},
}

// dockerRunning vrátí id běžícího kontejneru, "" když neběží nebo neexistuje.
func dockerRunning(name string) (string, error) {
	resp, err := docker.Get("http://docker/containers/" + name + "/json")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return "", nil
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("inspect %s: HTTP %d", name, resp.StatusCode)
	}
	var c struct {
		ID    string
		State struct{ Running bool }
	}
	if err := json.NewDecoder(resp.Body).Decode(&c); err != nil {
		return "", err
	}
	if !c.State.Running {
		return "", nil
	}
	return c.ID, nil
}

func cgroupAlive(path string) bool {
	b, err := os.ReadFile(filepath.Join(path, "cgroup.procs"))
	return err == nil && len(strings.TrimSpace(string(b))) > 0
}

func cgroupKill(path string) error {
	return os.WriteFile(filepath.Join(path, "cgroup.kill"), []byte("1"), 0)
}

// ids si pamatuje id kontejnerů z klidných chvil — když daemon pod tlakem
// neodpoví, pořád víme, kterou cgroup zabít.
var ids = map[string]string{}

func refreshIDs(cfg config) {
	for _, t := range cfg.targets {
		if name, ok := strings.CutPrefix(t, "docker:"); ok {
			if id, err := dockerRunning(name); err == nil && id != "" {
				ids[name] = id
			}
		}
	}
}

// killNext zabije první živou oběť ze seznamu. Vrátí její popis, "" když
// už není koho zabít.
func killNext(cfg config) (string, error) {
	var apiErr error // jakmile daemon jednou neodpoví, další cíle už ho nečekají
	for _, t := range cfg.targets {
		kind, what, _ := strings.Cut(t, ":")
		switch kind {
		case "docker":
			id, err := "", apiErr
			if apiErr == nil {
				id, err = dockerRunning(what)
				apiErr = err
			}
			if err != nil {
				// daemon neodpovídá — rozhodni podle cgroup známého kontejneru
				if known := ids[what]; known != "" {
					cg := "/sys/fs/cgroup/system.slice/docker-" + known + ".scope"
					if cgroupAlive(cg) {
						if cfg.dryRun {
							return t + " (cgroup, dry-run)", nil
						}
						return t + " (cgroup.kill, docker neodpovídá: " + err.Error() + ")", cgroupKill(cg)
					}
				}
				continue
			}
			if id == "" {
				continue
			}
			if cfg.dryRun {
				return t + " (dry-run)", nil
			}
			req, _ := http.NewRequest(http.MethodPost, "http://docker/containers/"+what+"/kill?signal=SIGKILL", nil)
			resp, err := docker.Do(req)
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode == http.StatusNoContent {
					return t, nil
				}
				err = fmt.Errorf("HTTP %d", resp.StatusCode)
			}
			cg := "/sys/fs/cgroup/system.slice/docker-" + id + ".scope"
			return t + " (cgroup.kill po chybě API: " + err.Error() + ")", cgroupKill(cg)
		case "cgroup":
			if !cgroupAlive(what) {
				continue
			}
			if cfg.dryRun {
				return t + " (dry-run)", nil
			}
			return t, cgroupKill(what)
		default:
			log.Printf("neznámý typ cíle %q, přeskakuji", t)
		}
	}
	return "", nil
}

// ── notifikace a restart ─────────────────────────────────────────────────────

func notify(cfg config, msg string) {
	log.Print(msg)
	if cfg.notify == "" || cfg.dryRun { // dry-run by při trvalém tlaku spamoval co 15 s
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "/bin/sh", "-c", cfg.notify+` "$1"`, "notify", msg)
		if out, err := cmd.CombinedOutput(); err != nil {
			log.Printf("notifikace selhala: %v %s", err, out)
		}
	}()
}

func rebootAllowed(cfg config) (bool, string) {
	b, err := os.ReadFile(cfg.rebootStamp)
	if err != nil {
		return true, ""
	}
	last, err := time.Parse(time.RFC3339, strings.TrimSpace(string(b)))
	if err != nil {
		return true, ""
	}
	if ago := time.Since(last); ago < cfg.rebootMinGap {
		return false, fmt.Sprintf("poslední restart guardem před %s", ago.Round(time.Second))
	}
	return true, ""
}

func rebootNow(cfg config) {
	_ = os.MkdirAll(filepath.Dir(cfg.rebootStamp), 0o755)
	_ = os.WriteFile(cfg.rebootStamp, []byte(time.Now().Format(time.RFC3339)+"\n"), 0o644)
	for _, k := range []string{"s", "u", "b"} { // sync, remount ro, reboot
		if err := os.WriteFile("/proc/sysrq-trigger", []byte(k), 0); err != nil {
			log.Printf("sysrq %s: %v", k, err)
		}
		time.Sleep(3 * time.Second)
	}
}

// ── hlavní smyčka ────────────────────────────────────────────────────────────

func main() {
	log.SetFlags(0) // čas přidá journald
	cfg := loadConfig()

	// Guard nesmí sám uvíznout na alokaci, když to nejvíc potřebujeme.
	if err := syscall.Mlockall(syscall.MCL_CURRENT); err != nil {
		log.Printf("mlockall: %v (pokračuji)", err)
	}

	log.Printf("start: min_avail=%dMiB/%s psi_full>=%.0f/%s psi_some>=%.0f/%s (psi jen pod %dMiB) reboot=%v dry_run=%v cíle=%s",
		cfg.minAvailMiB, cfg.availFor, cfg.psiFull, cfg.psiFullFor, cfg.psiSome, cfg.psiSomeFor, cfg.psiMaxAvail,
		cfg.reboot, cfg.dryRun, strings.Join(cfg.targets, " "))

	var (
		hAvail, hFull, hSome held
		lastKill, lastBeat   time.Time
		lastRefresh          time.Time
		exhaustedSince       time.Time
		rebootRefused        bool
	)
	tick := time.NewTicker(cfg.interval)
	defer tick.Stop()

	for now := range tick.C {
		s, err := readSample()
		if err != nil {
			log.Printf("čtení paměti: %v", err)
			continue
		}

		// PSI sám nestačí: director při startu čte 75 GiB vah a reclaim page cache
		// vyžene PSI přes 40 při 27 GiB volných (26. 9. ho to zabilo). Skutečný
		// nedostatek (25. 9., stress test) měl vždy MemAvailable 0–2 GiB.
		tight := s.availMiB < cfg.psiMaxAvail
		lowAvail := hAvail.update(now, s.availMiB < cfg.minAvailMiB, cfg.availFor)
		fullHi := hFull.update(now, tight && s.fullAvg >= cfg.psiFull, cfg.psiFullFor)
		someHi := hSome.update(now, tight && s.someAvg >= cfg.psiSome, cfg.psiSomeFor)
		trouble := lowAvail || fullHi || someHi

		if now.Sub(lastBeat) >= cfg.heartbeat {
			log.Printf("avail=%dMiB psi_some=%.2f psi_full=%.2f", s.availMiB, s.someAvg, s.fullAvg)
			lastBeat = now
		}
		if !trouble {
			exhaustedSince = time.Time{}
			rebootRefused = false
			if now.Sub(lastRefresh) >= time.Minute {
				refreshIDs(cfg)
				lastRefresh = now
			}
			continue
		}
		if now.Sub(lastKill) < cfg.cooldown {
			continue // dej předchozímu zabití čas uvolnit paměť
		}

		why := fmt.Sprintf("avail=%dMiB psi_some=%.1f psi_full=%.1f", s.availMiB, s.someAvg, s.fullAvg)
		victim, err := killNext(cfg)
		switch {
		case victim != "":
			lastKill = now
			exhaustedSince = time.Time{}
			if err != nil {
				notify(cfg, fmt.Sprintf("OOM guard: zabití %s SELHALO (%v) — %s", victim, err, why))
			} else {
				notify(cfg, fmt.Sprintf("OOM guard: zabit %s — %s", victim, why))
			}
		default:
			if exhaustedSince.IsZero() {
				exhaustedSince = now
				notify(cfg, "OOM guard: tlak trvá a na seznamu už nic neběží — "+why)
			}
			if !cfg.reboot || rebootRefused || now.Sub(exhaustedSince) < cfg.rebootAfter {
				continue
			}
			if ok, reason := rebootAllowed(cfg); !ok {
				rebootRefused = true
				notify(cfg, "OOM guard: restart bych potřeboval, ale "+reason+" — nechávám být, potřebuje ruku")
				continue
			}
			if cfg.dryRun {
				log.Printf("dry-run: tady by byl restart (%s)", why)
				rebootRefused = true
				continue
			}
			notify(cfg, fmt.Sprintf("OOM guard: tlak trvá %s bez oběti — restartuji stroj (%s)", now.Sub(exhaustedSince).Round(time.Second), why))
			time.Sleep(5 * time.Second) // ať notifikace stihne odejít
			rebootNow(cfg)
		}
	}
}
