#!/usr/bin/env bash
# SwarmBattle Nemotron models (3 of 4 pipeline tiers)
#
# Downloads:
#   Tier L3 (router/verifier): Nemotron-3-Nano-30B-A3B-NVFP4    ~15 GB
#   Tier L1 (director):        Nemotron-3-Super-120B-A12B-NVFP4  ~60 GB
#   Tier L4 (RAG/retrieval):   Llama-3.1-Nemotron-8B-UltraLong   ~16 GB
#   Custom parser:             nano_v3_reasoning_parser.py
#
# Usage: make download-nemotron  (reads .env for HF_TOKEN + cache paths)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${HF_TOKEN:?HF_TOKEN not set}"

CACHE_NANO="${CACHE_SWARM_NANO:-$ROOT/cache/swarm-nano}"
CACHE_DIRECTOR="${CACHE_SWARM_DIRECTOR:-$ROOT/cache/swarm-director}"
CACHE_RAG="${CACHE_SWARM_RAG:-$ROOT/cache/swarm-rag}"
PARSERS_DIR="$ROOT/deploy/parsers"

mkdir -p "$CACHE_NANO" "$CACHE_DIRECTOR" "$CACHE_RAG" "$PARSERS_DIR"

# ── Tier L3: Nano-30B router (NVFP4, ~15 GB) ──────────────────────────────
echo "[nemotron] downloading NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 → $CACHE_NANO"
hf download nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_NANO"

# ── Custom reasoning parser for Nano-30B ───────────────────────────────────
PARSER_URL="https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4/resolve/main/nano_v3_reasoning_parser.py"
echo "[nemotron] downloading nano_v3_reasoning_parser.py → $PARSERS_DIR"
curl -fsSL -H "Authorization: Bearer $HF_TOKEN" \
  "$PARSER_URL" \
  -o "$PARSERS_DIR/nano_v3_reasoning_parser.py"

# ── Tier L1: Super-120B director (NVFP4, ~60 GB) ──────────────────────────
echo "[nemotron] downloading NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 → $CACHE_DIRECTOR"
hf download nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_DIRECTOR"

# ── Tier L4: 8B-UltraLong RAG (BF16, ~16 GB) ──────────────────────────────
echo "[nemotron] downloading Llama-3.1-Nemotron-8B-UltraLong-4M-Instruct → $CACHE_RAG"
hf download nvidia/Llama-3.1-Nemotron-8B-UltraLong-4M-Instruct \
  --token "$HF_TOKEN" \
  --cache-dir "$CACHE_RAG"

echo "[nemotron] all downloads complete"
echo "[nemotron] parser at: $PARSERS_DIR/nano_v3_reasoning_parser.py"
