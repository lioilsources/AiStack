#!/usr/bin/env bash
# Qwen3-4B-AWQ (~2,5 GB) — překladač prompt-tags (próza → Danbooru tagy)
# Jiný model: HF_MODEL_PROMPT_TAGS v .env; galerie ho změří (tagbench).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${CACHE_PROMPT_TAGS:-$ROOT/cache/prompt-tags}"
MODEL="${HF_MODEL_PROMPT_TAGS:-Qwen/Qwen3-4B-AWQ}"
: "${HF_TOKEN:?HF_TOKEN not set}"

mkdir -p "$CACHE_DIR"
echo "[prompt-tags] downloading $MODEL → $CACHE_DIR"
hf download "$MODEL" \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIR"
echo "[prompt-tags] done"
