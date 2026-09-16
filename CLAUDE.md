# AiStack — CLAUDE.md

## Hardware
DGX Spark GB10, **128 GB unified LPDDR5X** (CPU+GPU sdílejí pool), aarch64, Ubuntu 24.04,
CUDA 13, compute capability **sm_121** (Blackwell).

## Spouštění
Vždy z `/home/ol1n/deploy/AiStack/` — jinak `.env` nenačte.

```bash
make up            # hlavní stack (LLM + gateway + controller + Cloudflare)
make up-llm        # jen LLM modul (dev NIM + litellm)
make up-dev        # jen dev kontejner (a litellm)
make up-swarm      # SwarmBattle stack
make ps            # stav kontejnerů
```

## Adresářová struktura

```
deploy/                       compose soubory, litellm konfigurace, reasoning parsery
  docker-compose.yml          root entrypoint: include llm + gateway, controller, cloudflared
  docker-compose.llm.yaml     dev (NIM), lab (NIM), litellm
  docker-compose.swarm.yaml   SwarmBattle: nano, coder, coder-nim, director, rag, embed
  docker-compose.translate.yaml
  docker-compose.tune-image.yaml
  docker-compose.ocr.yaml
  docker-compose.agent.yaml   qwen36-agent: Qwen3.6-35B-A3B NVFP4 pro OpenClaw (rezidentní)
  litellm_config.yaml         routovací tabulka pro hlavní gateway (dev/lab/translate/tune-*)
  litellm_config_swarm.yaml   routovací tabulka pro swarm-litellm
  parsers/                    custom reasoning parsery (nano_v3, nemotron_v3)

services/
  audio/                      Python: hudba + SFX (fronta, ffmpeg post-proc, SQLite)
    runtime/                  image modelových kontejnerů pro GB10 (ACE-Step, MOSS)
    scripts/                  download.sh, smoke_*, bench.py
  controller-manager/         Go: dynamic model switching přes docker.sock
    config/models.yaml        registr spravovaných stacků
  gen-queue/                  Go: async job queue pro FLUX NIM (cloudflared /nim/* → :8091)
  image-api/                  Python: FLUX.1-dev + Qwen image edit

pkg/audioclient/              Go klient audio API (používá ho Kirian pipeline)

cache/
  models/ngc/                 fyzická data NIM kontejnerů  (/opt/nim/.cache mount)
  models/hf/                  fyzická data HF/vLLM kontejnerů  (/root/.cache/huggingface/hub)
  {role} → symlink            cache/{role}/ ukazuje na models/{provider}/{model}/
  chromadb/                   ChromaDB vector store (SwarmBattle RAG)
  ocr/                        OCR model cache

scripts/
  download_*.sh               stažení HF modelů; volat přes `make download-*`

gateway/                      Go reverse proxy: :8080 → litellm:4000
cloudflared/                  Cloudflare tunnel credentials (llm.ol1n.com)
.env                          tokeny, cesty, verze — NIKDY commitovat
Makefile                      hlavní vstupní bod všech operací
```

## Cache konvence

Fyzická data žijí v `cache/models/{provider}/{model-slug}/`:
- `ngc/` — NIM kontejnery, mount jako `/opt/nim/.cache`
- `hf/` — vLLM kontejnery, mount jako `/root/.cache/huggingface/hub`

`cache/{role}/` jsou **symlinky** na výše uvedené adresáře.
Model swap = přepsat symlink + update `.env`. CLAUDE.md a compose soubory se nemění.

**NIM kontejnery nikdy nesdílejí cache dir** (kompilované engine artefakty, JIT cache).
**HF/vLLM kontejnery se stejným modelem cache sdílet mohou** (s `HF_HUB_OFFLINE=1` jen čtou).

Podrobnosti → `SKILL.md`.

## Image generation — gen-queue (`/nim/*`)

`services/gen-queue/` (Go) je robustní async job queue pro FLUX NIM modely.
Nahrazuje původní Python `nim-kontext-proxy`. Cloudflare routuje
`llm.ol1n.com/nim/*` přímo na `gen-queue:8091` (mimo hlavní gateway).

- `POST /nim/{flux-schnell|flux-kontext}/v1/infer` → `202 {id, queue_position}`
- `GET  /nim/{model}/jobs/{id}`        → `{status: queued|running|done|error}`
- `GET  /nim/{model}/jobs/{id}/result` → PNG bytes
- `GET  /health`                       → `{"status":"ok","service":"gen-queue"}`

Submit vrátí job_id okamžitě (obchází CF 100s edge timeout). Worker pool volá NIM
synchronně, retry na 5xx (3 pokusy 0/5/10 s), 4xx je non-retryable. Výsledky jsou
in-memory s TTL `RESULT_TTL_SECONDS` (default 3600 s); po TTL se evictuje
**výsledek i job-status** společně → `/jobs/{id}` i `/result` pak vrací 404. Stav
je čistě in-memory — restart gen-queue ztratí všechny joby.

Kontejnery `flux-schnell` / `flux-kontext` (NIM) se spouští přes
`docker-compose.image-nim.yaml` (`make up-image-schnell` / `up-image-kontext`).
Tok requestu sleduj přes `make logs-kontext` (`[cf]` → `[queue]` → `[nim]`).

## Audio generation — services/audio (`/v1/audio/*`)

Nahrazuje ElevenLabs pro Kirian. Tři kontejnery: lehký orchestrátor `audio`
(:8093, fronta + ffmpeg post-processing + SQLite) a dva modelové runtime
`audio-music` (ACE-Step 1.5, MIT) a `audio-sfx` (MOSS-SoundEffect v2.0,
Apache-2.0). Modely se zvedají a shazují přes controller
(`/ctrl/activate?model=audio-music`), orchestrátor běží pořád.

- `POST /v1/audio/music` / `/v1/audio/sfx` → `202 {job_id}`
- `GET  /v1/audio/jobs/{id}` → stav + výstupy (délka, LUFS, seed, sha256)
- `GET  /v1/audio/models` → katalog **včetně licencí** — Kirian jde na Steam,
  takže model bez komerční licence se do produkce nesmí dostat
- ElevenLabs shim `POST /v1/sound-generation`, `POST /v1/music/compose`
  (blokující, vrací audio) — jen pro přechodové období

Váhy leží mimo repo v `$AUDIO_MODELS_PATH` (default `/home/ol1n/dev/audio/models`),
ne v `/opt/audio` jak říkal plán — na SPARKu není passwordless sudo.
Detaily: `services/audio/README.md`, licence `services/audio/LICENSES.md`.

## Agent LLM — qwen36-agent (OpenClaw / PromoClown)

`deploy/docker-compose.agent.yaml`: nvidia/Qwen3.6-35B-A3B-NVFP4 na vLLM, v LiteLLM
jako `openclaw-default`. OpenClaw gateway běží na hostu a volá `http://127.0.0.1:8080/v1`
(gateway → litellm), protože litellm:4000 není na host publikovaný.

- **Rezidentní, mimo controller** — `/activate` shazuje předchozí model, agent
  si ale svoje okno drží sám (níž), takže ho controller nepřepíná.
- Tool calls `--tool-call-parser qwen3_xml` (ne hermes); thinking vypíná LiteLLM route.
- ~36 GB unified paměti (`AGENT_GPU_MEMORY_UTILIZATION=0.30`) — vLLM si je
  zabere při startu bez ohledu na zátěž a pod `util × total` volných odmítne
  nastartovat úplně.
- **Běží jen v okně promo 00–01**, jinak ho `rag-schedule.sh`
  (WorldLibraryProject, `AGENT_CONTAINERS`) zastaví. 121,7 GiB neuveze dva
  velké modely: vedle directora (0.75 = 91 GiB) ani vedle ComfyUI (52 GiB) se
  nevejde. Když v tom seznamu chyběl (11.–16. 9. 2026), director dvě noci po
  sobě nenaběhl a ComfyUI přes den počítalo na CPU. `restart: unless-stopped`
  je schválně: ruční `docker stop` vydrží, restart stroje ne.
- `make download-agent` → `make up-agent` → `curl localhost:8040/v1/models`.

## Porty (vše `127.0.0.1` pokud není uvedeno)

| port | kontejner | poznámka |
|------|-----------|---------|
| 8000 | lab | NIM |
| 8001 | dev | NIM |
| 8003 | ocr-api | NIM |
| 8004 | translate | NIM |
| 8091 | gen-queue | Go async job queue (FLUX NIM), interní — cloudflared /nim/* |
| 8093 | audio | `0.0.0.0` — orchestrátor hudby a SFX, přes gateway `/v1/audio/*` |
| 8094 | audio-music | ACE-Step 1.5 REST API (interní) |
| 8095 | audio-sfx | MOSS-SoundEffect / Stable Audio Open wrapper (interní) |
| 8005 | swarm-embed | vLLM, profile: embed |
| 8010 | swarm-nano | vLLM |
| 8011 | swarm-coder | vLLM (NGC image) |
| 8012 | swarm-director | vLLM, profile: director |
| 8013 | swarm-rag | vLLM, profile: rag |
| 8014 | swarm-coder-nim | NIM, profile: nim-coder |
| 8020 | tune-builder | vLLM |
| 8021 | tune-validator | NIM |
| 8040 | qwen36-agent | vLLM, rezidentní — OpenClaw přes LiteLLM `openclaw-default` |
| 4000 | litellm | hlavní gateway |
| 4001 | swarm-litellm | SwarmBattle gateway |
| 8080 | gateway | `0.0.0.0`, veřejný přes Cloudflare |
| 8090 | controller | Go service |
| 8188 | ComfyUI | host-native, mimo Docker |

## Důležitá varování

- **`--kv-cache-dtype fp8` nefunguje na GB10/Blackwell** — generuje tokenový šum. Nepoužívat.
- **`acrossfade`/`amix` nad `asplit` v jednom ffmpeg filtergraphu tiše zahodí prolnutí** —
  concat čte segmenty popořadě a větev s ocasem nikdy nedostane data. Bez chybové
  hlášky, jen kratší výstup. `services/audio` proto skládá smyčku přes dočasné soubory.
- **`huggingface-cli download` je deprecated** — v download skriptech používat `hf download`.
- `HF_HUB_OFFLINE=1` ve vLLM kontejnerech: model musí být stažen před startem, jinak selže.
- `make up-tune-image` **stopne `dev`** pro uvolnění paměti — `make up-llm` ho vrátí zpět.
- Controller manager montuje `/home/ol1n/deploy/AiStack/deploy` se stejnou cestou host↔kontejner
  (docker.sock předává host cestu daemonu).

## Síť

Jedna bridge síť `ai` (name: `ai`). Vytváří ji `docker-compose.yml`; všechny ostatní compose
soubory ji přebírají jako `external: true, name: ai`.

## .env — přehled klíčových proměnných

```
HF_TOKEN, NGC_API_KEY
VLLM_VERSION              # NGC vLLM image tag (pro swarm-coder a testy)
SWARM_VLLM_VERSION        # community vLLM image tag (pro swarm-nano, director, rag...)
HF_MODEL_{ROLE}           # HuggingFace model ID pro vLLM kontejner
CACHE_{ROLE}              # absolutní cesta na cache/{role}/ symlink
```

Každou novou proměnnou doplnit komentářem popisujícím roli.
