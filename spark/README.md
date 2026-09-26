# spark/ — OOM ochrana SPARKu

Cíl: **nedostatek paměti nesmí shodit host.** Umře GPU kontejner, SSH zůstane,
v nejhorším se stroj restartuje sám — ne rukou u zásuvky.

Proč to vzniklo a co se naměřilo: [`docs/oom-baseline.md`](docs/oom-baseline.md).
Zkráceně: na GB10 CUDA paměť není v RSS ani v cgroup kontejneru, takže kernelový
OOM killer, earlyoom i Docker `--memory` jsou vůči vLLM slepé. 25. 9. 2026 se
stroj dvě hodiny točil v OOM livelocku a zabíjel procesy s 1 MB RSS.

## Vrstvy

| vrstva | co dělá | kde |
|---|---|---|
| **mem-admit** | `make up-director-night` odmítne start, když `MemAvailable < util × total + 12 GiB` | `scripts/mem-admit.sh` |
| **spark-oom-guard** | hlavní pojistka: MemAvailable < 2 GiB 3 s, nebo PSI full ≥ 15 10 s, nebo PSI some ≥ 40 15 s → SIGKILL prvního živého cíle ze seznamu; když není koho zabít a tlak trvá 3 min → reboot (nejvýš 1× za hodinu) | `oom-guard/` |
| sysctl | `min_free_kbytes` 1 GiB, sysrq, swap tuning | `etc/sysctl.d/90-oom-guard.conf` |
| watchdog | SBSA watchdog přes systemd, 60 s — jen na skutečný hang kernelu/PID 1 | `etc/systemd/system.conf.d/` |
| protect | `OOMScoreAdjust=-1000` pro ssh, docker, containerd, NetworkManager, networkd, tailscaled | `etc/systemd/protect/` |
| swap | zram `ram/4` zstd (pri 100) + `/swap.img` 64 GiB (pri 10) — zpomalovač, ne řešení | `etc/systemd/zram-generator.conf` |

Pořadí cílů guardu je v `oom-guard/spark-oom-guard.env` (na stroji
`/etc/default/spark-oom-guard`): director → FLUX NIMy → ComfyUI → audio → tune/ocr/swarm
→ translate → qwen36-agent → fallback. Kontejner se zabíjí přes Docker API, takže
`restart: unless-stopped` ho nevrátí (ověřeno); ComfyUI přes `cgroup.kill`
(user unit má `Restart=always`, vrátí se prázdná za 10 s). Každé zabití jde
na Telegram přes `WorldLibraryProject/deploy/spark/notify.sh`.

## Nasazení

Binárka se staví na Macu (SPARK nemá Go):

```bash
make oom-guard-build
rsync -a --relative spark scripts/mem-admit.sh Makefile spark:deploy/AiStack/
ssh -t spark 'cd ~/deploy/AiStack/spark && sudo ./apply.sh'        # všechno
ssh -t spark 'cd ~/deploy/AiStack/spark && sudo ./apply.sh guard'  # jen jeden krok
```

Kroky: `sysctl watchdog protect swap guard`. Nic se nerestartuje (docker ani sshd),
zálohy přepsaných souborů jsou v `/var/backups/spark-oom-guard/<čas>/`.
`sudo GUARD_DRY_RUN=1 ./apply.sh guard` = guard jen loguje, nezabíjí ani nenotifikuje.

## Co z plánu (`OOM_GUARD_PLAN.md`) je jinak a proč

- **earlyoom / systemd-oomd nenasazeny.** Oba vybírají oběť podle RSS/cgroup paměti,
  ve které GPU alokace nejsou → zabíjeli by `library-chat` místo directora. Nahrazuje je
  vlastní guard (plán 5.3), který navíc umí reboot jako poslední krok.
- **Docker memory limity (Fáze 4) nenasazeny.** CUDA paměť do `memory.current`
  nepatří (flux-schnell: 17 GiB GPU, 1,7 GiB anon v cgroup) — limit by hlídal jen
  CPU stranu a hrozil by zabitím kontejneru při čtení vah do page cache.
- **vLLM vlajky (Fáze 3) beze změny.** Každá služba už má explicitní util,
  `max-model-len` a `max-num-seqs`, nikde `swap-space`/`cpu-offload`; FP8 KV je
  na GB10 zakázané (šum, CLAUDE.md). Director nejde pod ~0.72 — váhy mají 74 GiB.
  Problém nebyla jedna služba s 0.9, ale souběh (director + flux + ComfyUI).
- **„≥ 16 GiB volné pro host" platí jen mimo noční okno.** Director 0.75 nechává
  ~15–20 GiB; mem-admit s rezervou 12 GiB brání startu, kdy by to bylo míň.

## Provoz

```bash
journalctl -u spark-oom-guard -f                  # zásahy + heartbeat co minutu
journalctl -u spark-oom-guard | grep avail=       # historie MemAvailable/PSI
swapon --show; zramctl
sudo rm /var/lib/spark-oom-guard/last-reboot      # povolit další reboot dřív než za hodinu
MEM_ADMIT_FORCE=1 make up-director-night          # start i bez rezervy (vědomě)
```

## Zbývá (Fáze 6)

Hotovo: guard zabije docker i cgroup cíl, `unless-stopped` oběť nevrátí,
prázdný seznam → dry-run reboot, reboot blokovaný razítkem.
Nevyzkoušeno — ničí běžící služby nebo stroj, pouštět vědomě:

1. `stress-ng --vm 2 --vm-bytes 90% --timeout 60s` na hostu → guard musí zabít
   první GPU cíl, SSH musí žít.
2. `echo c | sudo tee /proc/sysrq-trigger` (kernel panic) → watchdog restartuje do ~60 s.
