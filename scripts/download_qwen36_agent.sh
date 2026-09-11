#!/usr/bin/env bash
# nvidia/Qwen3.6-35B-A3B-NVFP4 (HF, ~23,4 GB) — agent LLM pro OpenClaw.
#
# Usage: make download-agent  (reads .env for HF_TOKEN + CACHE_AGENT)
#
# Fyzická data do cache/models/hf/, cache/agent je symlink (konvence z CLAUDE.md).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${HF_TOKEN:?HF_TOKEN not set}"

MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
PHYSICAL="$ROOT/cache/models/hf/nvidia--Qwen3.6-35B-A3B-NVFP4"
LINK="${CACHE_AGENT:-$ROOT/cache/agent}"

mkdir -p "$PHYSICAL"
if [ ! -e "$LINK" ]; then
  ln -s "$PHYSICAL" "$LINK"
  echo "[agent] $LINK → $PHYSICAL"
fi

echo "[agent] downloading $MODEL → $PHYSICAL"
hf download "$MODEL" \
  --token "$HF_TOKEN" \
  --cache-dir "$PHYSICAL"

echo "[agent] done"
