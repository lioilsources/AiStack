# spark/monitoring — Grafana pro SPARK a JODU

Dashboard **SPARK + JODA** ve stylu L40S (TMobileSandbox `70_monitoring`):
GPU, RAM, swap, tlak na paměť (PSI), CPU, teploty, disky a síť obou strojů.

- Grafana: `http://<SPARK>:3000`. Anonymně jen čtení, admin heslo je v `.env` na SPARKu.
- Prometheus: jen `127.0.0.1:9090` na SPARKu, retence 90 dní / 5 GB.

## Proč na SPARKu a bez Dockeru

JODA nemá volnou RAM ani disk. Na SPARKu agenti nesmí sami pouštět kontejnery,
user systemd unity ano. Všechno proto běží jako user unity `mon-*` s binárkami
v `run/` (gitignored). Unity mají `MemoryMax`, aby je nezasáhl `spark-oom-guard`.
Když spadne SPARK, dashboard nepůjde otevřít, ale data do pádu zůstanou.

| unita | port | co |
|---|---|---|
| `mon-prometheus` | 127.0.0.1:9090 | sběr a úložiště |
| `mon-grafana` | 3000 | dashboard |
| `mon-gpu-exporter` | 127.0.0.1:9835 | `nvidia_gpu_exporter` (nvidia-smi) |
| systémový `prometheus-node-exporter` | 9100 | SPARK, už běžel |
| kontejner `node-exporter` na JODĚ | LAN:9100 | `joda/compose.yaml` |

GB10 má sjednocenou paměť: nvidia-smi nehlásí VRAM, GPU alokace jsou v RAM SPARKu.

## Nasazení

```bash
# z Macu, z kořene AiStack
rsync -a --relative spark/monitoring spark:deploy/AiStack/ --exclude run --exclude .env
ssh spark 'cd ~/deploy/AiStack/spark/monitoring && ./install.sh'
```

Poprvé na SPARKu vytvoř `.env` podle `.env.example`. `install.sh` stáhne verze
z `versions.env`, ověří SHA-256, vyrenderuje `prometheus.yml` a (re)startuje unity.

JODA:

```bash
scp spark/monitoring/joda/compose.yaml joda:deploy/node-exporter/
ssh joda 'cd ~/deploy/node-exporter && docker compose up -d'   # .env: JODA_LAN_IP
```

Docker na JODĚ je snap a hostitelský `/` do kontejneru nepřipojí (je z něj prázdný
tmpfs, `df` by hlásil 2 GB). Skutečný kořen hostitele je ve jmenném prostoru snapu
na `/var/lib/snapd/hostfs`, proto exporter připojuje ten a běží s `--path.rootfs=/host`.
Bez toho by `/` hlásil čísla úložiště Dockeru, které na JODĚ leží na ZFS.

## Úprava dashboardu

Dashboard se generuje: uprav `make_dashboard.py`, spusť ho a pošli `grafana/`
rsyncem. Grafana si změnu načte sama do 30 s. Ruční úpravy v UI se přepíší.
