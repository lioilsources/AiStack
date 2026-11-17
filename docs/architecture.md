# Architektura AiStacku

Přesunuto z `CLAUDE.md` (zkrácení na 80 řádků). Porty: [ports.md](ports.md), služby: [services.md](services.md).

## Hardware
DGX Spark GB10, **128 GB unified LPDDR5X** (CPU+GPU sdílejí pool), aarch64, Ubuntu 24.04,
CUDA 13, compute capability **sm_121** (Blackwell).

## Spouštění
Vždy z `~/deploy/AiStack/` na SPARKu, jinak `.env` nenačte (`--env-file .env` je relativní).
GPU služby na SPARKu přepíná `spark-broker` (`sparkctl`); `make up-*` volá broker jako
příkaz profilu nebo Olin při nasazení, ne session.

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
  audio/                      Python: hudba + SFX + TTS (fronta, ffmpeg post-proc, SQLite)
    runtime/                  image modelových kontejnerů pro GB10 (ACE-Step, MOSS, TTS)
    scripts/                  download.sh, smoke_*, bench.py
  controller-manager/         Go: dynamic model switching přes docker.sock
    config/models.yaml        registr spravovaných stacků
  gen-queue/                  Go: async job queue pro FLUX NIM (cloudflared /nim/* → :8091)
                              + image API /v1/image/* nad ComfyUI (IMAGE-API.md)
  nim-kontext-proxy/          Python, nahrazený gen-queue; compose ho nespouští
  spark-broker/               Go: fronta úloh SPARKu + sparkctl

pkg/audioclient/              Go klient audio API (používá ho Kirian pipeline)
spark/                        oom-guard, monitoring (Grafana/Prometheus) pro SPARK

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
.env                          tokeny, cesty, verze (necommituje se)
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

Každou novou proměnnou doplnit do `.env.example` s komentářem popisujícím roli.

## Další varování

- **`huggingface-cli download` je deprecated** — v download skriptech používat `hf download`.
- `make up-tune-image` **stopne `dev`** pro uvolnění paměti — `make up-llm` ho vrátí zpět.
- Controller manager montuje deploy adresář AiStacku (`~/deploy/AiStack/deploy`) se stejnou
  absolutní cestou host↔kontejner (docker.sock předává host cestu daemonu).
