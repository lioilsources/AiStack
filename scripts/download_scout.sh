#!/usr/bin/env bash
# Llama-4-Scout-17B-16E-Instruct-FP4 (nvidia HF repo, ~10 GB)
#
# Usage: make download-scout  (reads .env for HF_TOKEN + cache paths)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${HF_TOKEN:?HF_TOKEN not set}"

CACHE_DIR="${CACHE_SCOUT:-$ROOT/cache/models/hf/nvidia--Llama-4-Scout-17B-16E-Instruct-FP4}"
mkdir -p "$CACHE_DIR"

echo "[scout] downloading nvidia/Llama-4-Scout-17B-16E-Instruct-FP4 → $CACHE_DIR"
hf download nvidia/Llama-4-Scout-17B-16E-Instruct-FP4 \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIR"

echo "[scout] done"
