# SKILL — AiStack: provoz a stavba stack modulů

Referenční příručka: jak AiStack **provozovat** (up/down/logs), jeho **pravidla**,
rozdíl mezi **NIM** a **vLLM** moduly (+ úskalí), a checklist na **přidání nového** modulu.
Předpoklad: **vždy** pracovat z `/home/ol1n/deploy/AiStack/` (jinak se nenačte `.env`).

---

## A. Provoz — up / down / logs / ps

Vše přes `make` (zdroj pravdy = `Makefile`). `make ps` ukáže stav, `make logs` follow.

| Modul | nahodit | shodit | logy |
|-------|---------|--------|------|
| **celý stack** (LLM+gateway+controller+CF) | `make up` | `make down` | `make logs` / `make ps` |
| **LLM** (dev NIM + litellm) | `make up-llm` | `make down-llm` | `make logs-dev` |
| jen **dev** kontejner | `make up-dev` | `make down-dev` | `make logs-dev` |
| **translate** (vLLM) | `make up-translate` | `make down-translate` | `docker logs -f translate` |
| **swarm** (nano+coder+embed…) | `make up-swarm` | `make down-swarm` | `docker logs -f swarm-nano` |
| swarm **director** (on-demand) | `make up-swarm-director` | `make down-swarm-director` | `docker logs -f swarm-director` |
| **image NIM** (FLUX, 1 z 3) | `make up-image-{schnell\|kontext\|dev}` | `make down-image-nim` | `make logs-image` / `make logs-kontext` |
| **tune-image** (Card Forge) | `make up-tune-image` ¹ | `make down-tune-image` | `docker logs -f tune-builder` |
| **ocr** | `make up-ocr` | `make down-ocr` | `docker logs -f ocr-api` |

¹ `up-tune-image` **stopne `dev`** (uvolní paměť); zpět přes `make up-llm`.

Univerzální: `docker logs -f --tail 50 <kontejner>` · stav health: `docker inspect <c> --format '{{.State.Health.Status}}'`.
Po editaci `litellm_config.yaml` stačí restart gatewaye, ne celého stacku:
`docker compose -f deploy/docker-compose.llm.yaml restart litellm`.

---

## B. Pravidla AiStacku

- **Spouštět z `/home/ol1n/deploy/AiStack/`** — jinak `.env` nenačte (`--env-file .env` je relativní).
- **Model swap = přepsat symlink `cache/{role}` + `HF_MODEL_{ROLE}` v `.env`.** Compose ani CLAUDE.md se nemění.
- **NIM kontejnery NIKDY nesdílejí cache dir** (kompilované engine artefakty). HF/vLLM se stejným modelem cache sdílet **mohou** (s `HF_HUB_OFFLINE=1` jen čtou).
- **`--kv-cache-dtype fp8` NEFUNGUJE na GB10/Blackwell** — tokenový šum. Nepoužívat.
- **`huggingface-cli download` je deprecated** → `hf download`.
- **vLLM s `HF_HUB_OFFLINE=1`** — model musí být stažený **před** startem, jinak kontejner spadne.
- **Síť `ai`** (external) — vytváří ji jen `docker-compose.yml`, ostatní compose ji přebírají.
- **128 GB unified pool** (CPU+GPU sdílí) — před nasazením zkontroluj `make ps`, `nvidia-smi`, `free -g`.

---

## C. NIM vs vLLM — rozdíly a úskalí

|  | **NIM** | **vLLM** |
|--|---------|----------|
| image | `nvcr.io/nim/{org}/{model}:{tag}` | `vllm/vllm-openai:${SWARM_VLLM_VERSION}` |
| auth | `NGC_API_KEY` | `HF_TOKEN` |
| cache mount | `/opt/nim/.cache` | `/root/.cache/huggingface/hub` |
| model + engine | self-contained, NIM **stáhne sám** při 1. startu | váhy **předem** (`hf download`) + `HF_HUB_OFFLINE=1` |
| konfigurace | jen `NIM_*` env (nepřijímá vLLM args) | positional model + vLLM flagy v `command` |
| kvantizace | profil zabalený v image (NVFP4 auto) | autodetekce z `hf_quant_config.json` + NVFP4 env |

**Úskalí (z praxe):**

- **NIM dangling symlink → mount fail.** Když `cache/{role}` ukazuje na neexistující adresář, `docker` start spadne na `mkdir '…': file exists`. **Před `up` vytvořit cílový adresář:** `mkdir -p cache/models/ngc/{slug}` (NIM si ho pak naplní).
- **NGC download se umí zaseknout** (NET I/O stojí, cache neroste). Restart NIM sice resumuje, ale **duplikuje rozdělané shardy** v `…/tmp/` (každý 2×) → plýtvá místem. Při opakovaném stallu **přejít na HF/vLLM ekvivalent** (stabilnější CDN, např. `nvidia/Qwen3-32B-FP4` místo NIM `qwen3-32b-dgx-spark`). Sledovat: `du -sh cache/{role}/`, `docker stats <c>`.
- **NIM neloguje průběh stahování** — `Downloaded filename:` zaloguje až po **dokončení** shardu; ticho v logu ≠ zaseknuté. Ověřit přes `du`/`docker stats`, ne podle logu.
- **NVFP4 env (vLLM):** dense modely → `VLLM_NVFP4_GEMM_BACKEND=marlin`; MoE modely → `VLLM_USE_FLASHINFER_MOE_FP4=1` + `VLLM_FLASHINFER_MOE_BACKEND=throughput`.
- **Vypnutí reasoningu** (Qwen3/Nemotron) řešit v `litellm_config.yaml` přes `extra_body.chat_template_kwargs.enable_thinking: false` — ne `/no_think` tokenem v promptu.

---

## 0. Rozhodnutí před začátkem

| otázka | implikace |
|--------|-----------|
| NIM nebo vLLM? | NIM: `NGC_API_KEY`, jednodušší konfigurace, NGC stáhne váhy sám; vLLM: `HF_TOKEN`, plná kontrola nad argumenty |
| Stálý nebo on-demand? | on-demand → `profiles: ["{name}"]`, nespouští se s `make up-{modul}` |
| Sdílí model s existujícím stackem? | HF/vLLM: sdílet cache symlink (viz krok 1); NIM: **nikdy nesdílet** |
| Potřebuje custom reasoning parser? | ano → přidat `.py` do `deploy/parsers/`, mountovat jako `:ro` |

---

## 1. Cache — nový adresář a symlink

```bash
# HF/vLLM model
mkdir -p cache/models/hf/{org}--{model-slug}
# stáhnout váhy (viz krok 6)

# NIM model — adresář vytvořit, NIM si stáhne sám při prvním startu
mkdir -p cache/models/ngc/{org}--{model-slug}

# Symlink: role → fyzický adresář
ln -s models/{ngc|hf}/{org}--{model-slug} cache/{role}
```

Formát slugu: `{lowercase-org}--{model-name}` (dvojitá pomlčka jako oddělovač, bez verze).
Příklady: `nvidia--Nemotron-Nano-30B-A3B-NVFP4`, `meta--llama-3.1-8b-instruct`.

Do `.env` přidat:
```bash
CACHE_{ROLE}=/home/ol1n/deploy/AiStack/cache/{role}
HF_MODEL_{ROLE}={org}/{model}     # jen pro HF/vLLM (NIM nepotřebuje)
```

---

## 2. Compose service — šablony

Soubor: `deploy/docker-compose.{module}.yaml`

**Povinná hlavička:**
```yaml
# Popis modulu — co dělá, kdy se spouští
#
# Standalone: docker compose -f deploy/docker-compose.{module}.yaml up -d
# Env vars:   HF_TOKEN/NGC_API_KEY, CACHE_{ROLE} [, HF_MODEL_{ROLE}]
```

### NIM

```yaml
{role}:
  image: nvcr.io/nim/{org}/{model}:{tag}
  container_name: {role}
  restart: unless-stopped
  # profiles: ["{profile-name}"]   # pokud on-demand
  environment:
    - NGC_API_KEY=${NGC_API_KEY}
    - NIM_SERVED_MODEL_NAME={role}
    # - NIM_MAX_MODEL_LEN=32768
    # - NIM_GPU_MEMORY_UTILIZATION=0.30
  ports:
    - "127.0.0.1:{port}:8000"
  shm_size: 16gb
  volumes:
    - ${CACHE_{ROLE}}:/opt/nim/.cache
  networks:
    - internal
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
  healthcheck:
    test: ["CMD", "curl", "-sf", "http://localhost:8000/v1/models"]
    interval: 30s
    timeout: 10s
    retries: 5
    start_period: 300s    # 600s pro modely > 40 GB
```

### vLLM (community image)

```yaml
{role}:
  image: vllm/vllm-openai:${SWARM_VLLM_VERSION}
  container_name: {role}
  restart: unless-stopped
  # profiles: ["{profile-name}"]
  command: >
    ${HF_MODEL_{ROLE}}
    --served-model-name {role}
    --gpu-memory-utilization 0.XX
    --max-model-len XXXXX
    --trust-remote-code
    # --enable-auto-tool-choice --tool-call-parser qwen3_coder
    # --reasoning-parser nemotron_v3
    # --reasoning-parser-plugin /parsers/{parser}.py
    # --reasoning-parser {parser-name}
  environment:
    - HF_TOKEN=${HF_TOKEN}
    - HF_HUB_OFFLINE=1
    # - VLLM_USE_FLASHINFER_MOE_FP4=1     # MoE modely
    # - VLLM_FLASHINFER_MOE_BACKEND=throughput
    # - VLLM_NVFP4_GEMM_BACKEND=marlin    # NVFP4 modely
  ports:
    - "127.0.0.1:{port}:8000"
  shm_size: 8gb
  volumes:
    - ${CACHE_{ROLE}}:/root/.cache/huggingface/hub
    # - ./parsers:/parsers:ro
  networks:
    - internal
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
  healthcheck:
    test: ["CMD", "curl", "-sf", "http://localhost:8000/v1/models"]
    interval: 30s
    timeout: 10s
    retries: 5
    start_period: 300s
```

**Nikdy nepoužívat `--kv-cache-dtype fp8`** — tokenový šum na GB10/Blackwell.

### Síťová sekce (vždy na konci souboru)

```yaml
networks:
  internal:
    external: true
    name: ai
```

(Síť vytváří jen `docker-compose.yml`, všechny ostatní ji přebírají jako external.)

---

## 3. LiteLLM registrace

`deploy/litellm_config.yaml` (pro hlavní gateway) nebo `litellm_config_swarm.yaml` (SwarmBattle):

```yaml
- model_name: {role}
  litellm_params:
    model: openai/{role}
    api_base: http://{role}:8000/v1
    api_key: dummy
    timeout: 300
    # extra_body:
    #   chat_template_kwargs:
    #     enable_thinking: false
```

Po změně — restartovat **jen litellm**, ne celý stack:
```bash
docker compose -f deploy/docker-compose.llm.yaml restart litellm
```

---

## 4. Controller manager (jen pokud model řídí controller)

`services/controller-manager/config/models.yaml`:

```yaml
{role}:
  compose_file: /home/ol1n/deploy/AiStack/deploy/docker-compose.{module}.yaml
  services: [{role}]
  health_url: http://{role}:8000/v1/models
  health_timeout: 360s
  alias: {role}
```

Cesta musí být **absolutní a identická** host↔kontejner (controller volá docker.sock s host cestami).

---

## 5. Makefile

```makefile
COMPOSE_{NAME} := docker compose -f deploy/docker-compose.{module}.yaml --env-file .env

up-{module}:
	$(COMPOSE_{NAME}) up -d

down-{module}:
	$(COMPOSE_{NAME}) down
```

S download targetem (jen HF modely):
```makefile
download-{module}:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_{module}.sh
```

---

## 6. Download script (jen HF/vLLM modely)

`scripts/download_{module}.sh`:
```bash
#!/usr/bin/env bash
# Stručný popis — co stahuje, přibližné velikosti
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${HF_TOKEN:?HF_TOKEN not set}"

CACHE_DIR="${CACHE_{ROLE}:-$ROOT/cache/{role}}"
mkdir -p "$CACHE_DIR"

echo "[{module}] downloading {org}/{model} → $CACHE_DIR"
hf download {org}/{model} \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIR"
# Pozn.: používat `hf download` — `huggingface-cli download` je deprecated

echo "[{module}] done"
```

Spouštět výhradně přes Makefile (ten exportuje `.env`):
```bash
make download-{module}
```

---

## 7. Paměťový rozpočet

DGX Spark má 128 GB unified pool (CPU+GPU sdílejí). Zkontrolovat před nasazením:

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
free -g
```

Stálé kontejnery baseline (aktuální stav):

| kontejner | přibližně |
|-----------|----------|
| dev (llama-3.1-8b FP8 NIM) | ~67 GB |
| litellm | < 1 GB |
| gateway, controller, cloudflared | < 1 GB dohromady |

Zbývá pro nové kontejnery: **~60 GB** (bez swarm stacku).
Se swarm stackem (nano+coder+embed resident): ~60 − 25 GB = ~35 GB.

---

## 8. Verifikace

```bash
# Kontejner naběhl a je healthy
docker ps --filter name={role}

# Model API odpovídá
curl -s http://localhost:{port}/v1/models | python3 -m json.tool

# Přes hlavní gateway
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"{role}","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
  | jq .choices[0].message.content

# Cache symlink ukazuje správně
ls -la cache/{role}
readlink -f cache/{role}
```
