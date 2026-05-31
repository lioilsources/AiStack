#!/usr/bin/env bash
# Qwen2.5-VL-7B-Instruct (~17 GB) — OCR model
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${CACHE_QWEN_VL:-$ROOT/cache/qwen-vl}"
: "${HF_TOKEN:?HF_TOKEN not set}"

mkdir -p "$CACHE_DIR"
echo "[qwen-vl] downloading Qwen/Qwen2.5-VL-7B-Instruct → $CACHE_DIR"
hf download Qwen/Qwen2.5-VL-7B-Instruct \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIR"
echo "[qwen-vl] done"
