#!/usr/bin/env bash
# Whisper large-v3-turbo (~1,6 GB) — ASR (deploy/docker-compose.asr.yaml)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${CACHE_WHISPER:-$ROOT/cache/whisper}"
: "${HF_TOKEN:?HF_TOKEN not set}"

mkdir -p "$CACHE_DIR"
echo "[whisper] downloading openai/whisper-large-v3-turbo → $CACHE_DIR"
hf download openai/whisper-large-v3-turbo \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIR"
echo "[whisper] done"
