#!/usr/bin/env bash
# SwarmBattle coder model — Tier L2 of Pipeline B
#
# Two options (set SWARM_CODER_VARIANT in env):
#   phi4    (default) — microsoft/Phi-4         ~14 GB   conservative, fast
#   nemotron         — nvidia/Llama-3_3-Nemotron-Super-49B-v1  ~28 GB Q4  better reasoning
#
# Usage: make download-nemotron-coder
#        SWARM_CODER_VARIANT=nemotron make download-nemotron-coder
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${HF_TOKEN:?HF_TOKEN not set}"

VARIANT="${SWARM_CODER_VARIANT:-phi4}"
CACHE_CODER="${CACHE_SWARM_CODER:-$ROOT/cache/swarm-coder}"

mkdir -p "$CACHE_CODER"

case "$VARIANT" in
  phi4)
    echo "[swarm-coder] downloading microsoft/Phi-4 → $CACHE_CODER"
    hf download microsoft/Phi-4 \
      --token "$HF_TOKEN" \
      --cache-dir "$CACHE_CODER"
    echo "[swarm-coder] done — set HF_MODEL_SWARM_CODER=microsoft/Phi-4 in .env"
    ;;

  nemotron)
    # Llama-3.3-Nemotron-Super-49B-v1: dense 49B, MBPP 91.3%, 128K context
    # In vLLM BF16 ~98 GB; use a GGUF Q4_K_M quant (~28 GB) instead.
    # Recommended quant: bartowski/Llama-3.3-Nemotron-Super-49B-v1-GGUF (Q4_K_M)
    CACHE_HEAVY="${CACHE_SWARM_CODER_HEAVY:-$ROOT/cache/swarm-coder-heavy}"
    mkdir -p "$CACHE_HEAVY"
    echo "[swarm-coder] downloading Llama-3.3-Nemotron-Super-49B-v1 Q4_K_M → $CACHE_HEAVY"
    echo "[swarm-coder] NOTE: vLLM can serve GGUF — or use BF16 on DGX Spark (98 GB, no room for director)"
    hf download bartowski/Llama-3.3-Nemotron-Super-49B-v1-GGUF \
      --include "Llama-3.3-Nemotron-Super-49B-v1-Q4_K_M.gguf" \
      --token "$HF_TOKEN" \
      --cache-dir "$CACHE_HEAVY"
    echo "[swarm-coder] done — set HF_MODEL_SWARM_CODER=<path-to-gguf> in .env"
    ;;

  *)
    echo "Unknown SWARM_CODER_VARIANT='$VARIANT'. Use: phi4 | nemotron" >&2
    exit 1
    ;;
esac
