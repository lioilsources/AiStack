# AiStack — CLAUDE.md

## Účel
AI stack na SPARKu (DGX Spark GB10, aarch64, 128 GB unified, sm_121): NIM/vLLM modely
za LiteLLM a Go gateway, fronty pro obrázky (gen-queue) a audio, spark-broker pro
sdílení GPU mezi projekty.

## Build a test
```
cd services/spark-broker && make test                # go test -race ./...
cd services/controller-manager && make test
cd services/gen-queue && go test ./...               # stejně pkg/audioclient
python3 -m pytest services/audio/tests
make gateway-build | make oom-guard-build            # oom-guard pro SPARK staví Mac
```
Nasazení: `git pull` v `~/deploy/AiStack/` na SPARKu, vždy z kořene (jinak `.env` nenačte).
Image se buildí na SPARKu (aarch64), build z Macu nefunguje.

## Mapa
- `deploy/` — compose soubory, `litellm_config*.yaml`, reasoning parsery
- `services/` — `audio` (Py), `controller-manager`, `gen-queue`, `spark-broker` (Go)
- `gateway/` — Go reverse proxy :8080 → litellm:4000
- `spark/` — oom-guard, monitoring; `pkg/audioclient/` — Go klient audio API
- `scripts/download_*.sh` — přes `make download-*`; `Makefile` je vstupní bod
- `cache/` — modely mimo git, `cache/{role}` jsou symlinky (`SKILL.md`)

## Invarianty
- **SPARK přepíná jen spark-broker** (`sparkctl`). Profily `comfy`, `llm`, `gemma`,
  `director`, `gptoss` a doplňky jsou v `services/spark-broker/deploy/config.example.yaml`.
  Žádný ruční `docker stop`/`start`, žádné pevné okno `rag-schedule.timer`.
- Rozvrhem řízené kontejnery nikdy `compose down` (`make down-agent` 30. 9. 2026
  smazal qwen36-agent). ComfyUI ani director se s velkým LLM nevejdou.
- **`--kv-cache-dtype fp8` nefunguje na GB10/Blackwell** — tokenový šum. Nepoužívat.
- **`acrossfade`/`amix` nad `asplit` v jednom ffmpeg filtergraphu tiše zahodí prolnutí**
  (concat čte segmenty popořadě). `services/audio` proto skládá smyčku přes dočasné soubory.
- `HF_HUB_OFFLINE=1` ve vLLM kontejnerech: model musí být stažen před startem.
- NIM kontejnery nikdy nesdílejí cache dir; HF/vLLM se stejným modelem mohou.
- Audio/TTS model bez komerční licence nesmí do produkce (`/v1/audio/models`,
  `services/audio/LICENSES.md`); XTTS-v2 je nekomerční.
- Nová proměnná v `.env` = řádek s komentářem v `.env.example`.
- Síť `ai` vytváří `deploy/docker-compose.yml`, ostatní compose ji berou jako external.

## Kam dál
- `docs/architecture.md` — hardware, struktura, cache, síť, `.env`, další varování
- `docs/services.md` — gen-queue, audio + TTS, qwen36-agent, spark-broker
- `docs/ports.md` — porty a bindy
- `SKILL.md` — cache konvence a controller; `docs/IMAGE-GEN-GUIDE.md` — obrázky
- `services/*/README.md`, `PLAN-*.md`, `Prompts/`
