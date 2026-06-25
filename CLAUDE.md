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
  litellm_config.yaml         routovací tabulka pro hlavní gateway (dev/lab/translate/tune-*)
  litellm_config_swarm.yaml   routovací tabulka pro swarm-litellm
  parsers/                    custom reasoning parsery (nano_v3, nemotron_v3)

services/
  controller-manager/         Go: dynamic model switching přes docker.sock
    config/models.yaml        registr spravovaných stacků
  image-api/                  Python: FLUX.1-dev + Qwen image edit

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

## Porty (vše `127.0.0.1` pokud není uvedeno)

| port | kontejner | poznámka |
|------|-----------|---------|
| 8000 | lab | NIM |
| 8001 | dev | NIM |
| 8003 | ocr-api | NIM |
| 8004 | translate | NIM |
| 8005 | swarm-embed | vLLM, profile: embed |
| 8010 | swarm-nano | vLLM |
| 8011 | swarm-coder | vLLM (NGC image) |
| 8012 | swarm-director | vLLM, profile: director |
| 8013 | swarm-rag | vLLM, profile: rag |
| 8014 | swarm-coder-nim | NIM, profile: nim-coder |
| 8020 | tune-builder | vLLM |
| 8021 | tune-validator | NIM |
| 4000 | litellm | hlavní gateway |
| 4001 | swarm-litellm | SwarmBattle gateway |
| 8080 | gateway | `0.0.0.0`, veřejný přes Cloudflare |
| 8090 | controller | Go service |
| 8188 | ComfyUI | host-native, mimo Docker |

## Důležitá varování

- **`--kv-cache-dtype fp8` nefunguje na GB10/Blackwell** — generuje tokenový šum. Nepoužívat.
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
