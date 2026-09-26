# OOM baseline — SPARK (Fáze 0), 2026-09-26

Změřeno 2 min po ručním power-cyclu (stroj ležel od 25. 9. večer). Nic se neměnilo.

## Systém

| | |
|---|---|
| kernel | 6.17.0-1029-nvidia, aarch64, Ubuntu 24.04.4 |
| RAM | 121,7 GiB (MemTotal 127 600 816 kB), unifikovaná CPU+GPU |
| swap | `/swap.img` 16 GiB, prio -2; žádný zram; zswap **vypnutý** (`N`) |
| sysctl | `overcommit_memory=0`, `swappiness=60`, `min_free_kbytes=45167` (44 MiB), `admin_reserve_kbytes=8192`, `sysrq=176`, `panic_on_oom=0`, `watermark_scale_factor=10`, `watermark_boost_factor=15000`, `page-cluster=3` |
| OOM démoni | `systemd-oomd` ani `earlyoom` neběží (earlyoom není nainstalovaný) |
| watchdog | `/dev/watchdog0` = **SBSA Generic Watchdog**, timeout 10 s; systemd `RuntimeWatchdogUSec=0` (vypnuto) |
| sudo | bez hesla nejde — všechno root-only jde přes `sudo ./apply.sh` |
| disk | `/` 3,7 TB, volných 553 GB |

## Otázka 3 z plánu: počítá se CUDA paměť do cgroup kontejneru?

**Ne.** Běžící `flux-schnell` (NIM):

| zdroj | hodnota |
|---|---|
| `nvidia-smi` used_memory (pid 9015) | 17 037 MiB |
| cgroup `memory.current` | 16 862 MiB |
| z toho `anon` | **1 716 MiB** |
| z toho `file` (page cache vah) | 15 025 MiB |
| VmRSS procesu | 1 745 MiB |

`memory.current` vypadá podobně jen náhodou — je to page cache ze čtení vah,
ne GPU alokace. Stejně tak u host procesů (ComfyUI, RAG servery): GPU paměť
není v RSS. Důsledky:

- Docker `--memory` limit **GPU stranu nehlídá** → Fáze 4 by chránila jen CPU
  stranu (tokenizer, python heap). Nenasazeno, viz README.
- OOM killer i earlyoom vybírají oběť podle RSS → vyberou špatně.
- `MemAvailable` v `/proc/meminfo` pokles od driveru **vidí** (121,7 − 76,6 GiB
  při tomto měření sedí na součet GPU + anon + neuvolnitelné) → signál pro guard.

## Pád 25. 9. 2026 (`journalctl -k -b -1`)

- 24. 9. 05:48/05:55 a 25. 9. 09:58 — OOM killer zabil `law-chat`, `library-chat`
  a ComfyUI (39 GiB anon při CPU renderu). Ty se daly zabít a pomohlo to.
- **25. 9. 19:35** — první `task … blocked for more than 122 seconds`
  (`nvidia-smi`, `dashboard-admin`, `jbd2`, `cache_mgr_main`) — driver drží zámky.
- **20:09–21:56** — OOM killer běží opakovaně, oběti mají **1,3 MiB anon RSS**
  (`law-chat`, `library-chat`). Meminfo v 20:09: `active_anon+inactive_anon`
  ≈ 0,5 GiB, `file` ≈ 30 MiB, `Normal free` 44 MiB pod `min`, swap 16 GiB
  plný (volných 380 MiB). Zbylých ~120 GiB tedy drží NVIDIA driver mimo LRU —
  kernel neměl co uvolnit.
- 21:56:49 poslední zápis (`systemd-journald: Under memory pressure`), pak ticho
  do power-cyclu 26. 9. 11:19.

Stroj tedy nebyl mrtvý na kernelové úrovni — dvě hodiny se točil v OOM
livelocku. Hardwarový watchdog by nezasáhl (PID 1 žil), sshd nedokončil banner.

## Paměť po bootu (bez zásahu)

Po startu se samy zvedly `flux-schnell`, `audio-music`, `audio-sfx`, ComfyUI
(system **i** user unit), `library-chat` (system **i** user unit), `law-chat`,
`ugc-sf3d`, postiz stack, litellm, gateway… → 44 GiB used, 76 GiB available.

## Co bude ověřit pod zátěží

- noční profil (director 0.75): MemAvailable a PSI během běhu obohacení —
  guard je loguje každou minutu (`journalctl -u spark-oom-guard | grep avail=`)
- ~~zda práh guardu nesedí příliš blízko nočního dna~~ — seděl: v režimu rag
  (26. 9. 12:30) je MemAvailable 95 GiB před startem directora, po něm ~4 GiB.
  Práh snížen 2048 → 1024 MiB, rezerva mem-admit 12 → 2 GiB.
- Start directora (26. 9. 12:40–12:42): čtení vah drží PSI some/full 20–46
  při 26–29 GiB volných; po alokaci KV cache (23,3 GiB) ustálený stav
  **3,4 GiB volných**, PSI ještě ~17 (doznívající avg10). PSI podmínky guardu
  proto platí jen pod 2 GiB.
