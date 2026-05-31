#!/usr/bin/env bash
# FLUX.1-dev (~24 GB) — text-to-image generation
# Gated model: accept license at https://huggingface.co/black-forest-labs/FLUX.1-dev first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${CACHE_FLUX:-$ROOT/cache/flux}"
: "${HF_TOKEN:?HF_TOKEN not set}"

mkdir -p "$CACHE_DIR"
echo "[flux] downloading black-forest-labs/FLUX.1-dev → $CACHE_DIR"
hf download black-forest-labs/FLUX.1-dev \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIR"
echo "[flux] done"
