# Swarm stack — migrace na NVIDIA image

Testujeme tři přístupy postupně na `swarm-coder`, pak rozjedeme ostatní role.

---

## Cache stav (swarm-coder)

```
cache/swarm-coder/
  models--nvidia--Llama-3_3-Nemotron-Super-49B-v1/          # BF16 base ✓ v cache
    snapshots/387156d8d6868c19f3472fa607aa9bfc4f662333/
  models--nvidia--Llama-3_3-Nemotron-Super-49B-v1-FP8/      # FP8 ✓ v cache
  # NVFP4 varianta CHYBÍ — proto vLLM padal (HF_HUB_OFFLINE=1)
```

---

## Experiment A — NIM default (engine z NGC)

NIM si stáhne model + zkompiluje TRT-LLM engine sama. Žádný lokální model potřeba.

```yaml
swarm-coder:
  image: nvcr.io/nim/nvidia/llama-3.3-nemotron-super-49b-v1:latest
  environment:
    - NGC_API_KEY=${NGC_API_KEY}
    - NIM_SERVED_MODEL_NAME=swarm-coder
  volumes:
    - ${CACHE_SWARM_CODER}:/opt/nim/.cache      # engine se uloží sem pro příští run
  shm_size: 16gb
  healthcheck:
    start_period: 600s    # první run: kompilace enginu ~10–30 min (issue #5250)
```

**Prerequisit (jednou):**
```bash
docker login nvcr.io -u '$oauthtoken' --password $NGC_API_KEY
```

**Spuštění:**
```bash
docker compose --project-directory . -f deploy/docker-compose.swarm.yaml up -d swarm-coder
docker logs -f swarm-coder
```

**Co sledovat:** `Uvicorn running` nebo `NIM server started`

---

## Experiment B — NIM s lokálním HF modelem

NIM dostane BF16 base model z lokální cache → zkompiluje vlastní TRT-LLM engine.
Model je **již v cache** (`387156d8d6868c19f3472fa607aa9bfc4f662333`).

```yaml
swarm-coder:
  image: nvcr.io/nim/nvidia/llama-3.3-nemotron-super-49b-v1:latest
  environment:
    - NGC_API_KEY=${NGC_API_KEY}
    - NIM_SERVED_MODEL_NAME=swarm-coder
    - NIM_MODEL_NAME=/hf-model
  volumes:
    - ${CACHE_SWARM_CODER}/models--nvidia--Llama-3_3-Nemotron-Super-49B-v1/snapshots/387156d8d6868c19f3472fa607aa9bfc4f662333:/hf-model:ro
    - ${CACHE_SWARM_CODER}:/opt/nim/.cache
  shm_size: 16gb
  healthcheck:
    start_period: 600s
```

**Co se liší od A:** NIM nestahuje model z NGC, jen kompiluje engine z lokálních vah.

---

## Experiment C — NGC vLLM s HF modelem (drop-in)

Zachová veškeré vLLM CLI argumenty (tool-call-parser, reasoning-parser, atd.).
Používáme **FP8** variantu (v cache, ~49 GB) nebo NVFP4 po stažení (~24 GB).

```yaml
swarm-coder:
  image: nvcr.io/nvidia/vllm:26.03.post1-py3    # NGC build = VLLM_VERSION z .env
  command: >
    nvidia/Llama-3_3-Nemotron-Super-49B-v1-FP8
    --served-model-name swarm-coder
    --gpu-memory-utilization 0.40
    --max-model-len 32768
    --enable-auto-tool-choice
    --tool-call-parser pythonic
    --trust-remote-code
  environment:
    - HF_TOKEN=${HF_TOKEN}
    - HF_HUB_OFFLINE=1
  volumes:
    - ${CACHE_SWARM_CODER}:/root/.cache/huggingface/hub
  shm_size: 8gb
  healthcheck:
    start_period: 300s
```

> **Poznámka:** NVFP4 varianta není v cache. FP8 je k dispozici (gpu_util ~40%).
> Pro NVFP4 (24 GB, gpu_util 0.20) spusť download níže.

---

## HF download příkazy

```bash
# NVFP4 coder (24 GB) — potřeba pro vLLM Marlin backend
huggingface-cli download nvidia/Llama-3_3-Nemotron-Super-49B-v1-NVFP4 \
  --cache-dir cache/swarm-coder \
  --token $HF_TOKEN

# Base BF16 (98 GB) — potřeba jen pokud NIM_MODEL_NAME nefunguje s FP8/NVFP4
# (pravděpodobně již staženo: models--nvidia--Llama-3_3-Nemotron-Super-49B-v1 ✓)
```

---

## Roadmapa ostatních rolí

Po otestování coder přístupů rozjedeme ostatní. Modely k potvrzení:

| Role | Aktuální HF model | NIM image (ověřit) | NGC vLLM |
|------|------------------|-------------------|----------|
| swarm-nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4` | `nvcr.io/nim/nvidia/nemotron-3-nano:latest` | drop-in + zachová `nano_v3_reasoning_parser.py` |
| swarm-director | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | hledat na build.nvidia.com | NIM vynechá problematický `--kv-cache-dtype fp8` |
| swarm-rag | `nvidia/Llama-3.1-Nemotron-8B-UltraLong-4M-Instruct` | pravděpodobně není v NIM | NGC vLLM drop-in |
| swarm-embed | `intfloat/e5-mistral-7b-instruct` | embedding NIM existuje | NGC vLLM `--task embed` |

**Ověřit existenci NIM image:**
```bash
docker pull nvcr.io/nim/nvidia/nemotron-3-nano:latest
docker pull nvcr.io/nim/nvidia/nemotron-super-120b-a12b:latest  # director — název neověřen
```

---

## Verifikace každého experimentu

```bash
# Ověřit model name
curl -s http://127.0.0.1:8011/v1/models | python3 -m json.tool

# Test tool call
curl -s http://127.0.0.1:8011/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"swarm-coder","messages":[{"role":"user","content":"Write a Python hello world"}],"max_tokens":64}'
```
