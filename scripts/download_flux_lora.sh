#!/usr/bin/env bash
# FLUX.1-dev NSFW LoRA (~690 MB) — xey/sldr_flux_nsfw_v2-studio
# Downloaded next to the base FLUX model so it shares the same mounted cache dir
# ($CACHE_FLUX → /root/.cache/huggingface/flux). A stable lora.safetensors symlink
# points at the source file, so FLUX_NSFW_LORA stays fixed regardless of repo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${CACHE_FLUX:-$ROOT/cache/flux}"
LORA_REPO="${FLUX_NSFW_LORA_REPO:-xey/sldr_flux_nsfw_v2-studio}"
LORA_FILE="${FLUX_NSFW_LORA_FILE:-sldr_flux_nsfw_v2-studio.safetensors}"
LORA_DIR="$CACHE_DIR/lora"
: "${HF_TOKEN:?HF_TOKEN not set}"

mkdir -p "$LORA_DIR"
echo "[flux-lora] downloading $LORA_REPO/$LORA_FILE → $LORA_DIR"
hf download "$LORA_REPO" "$LORA_FILE" \
  --token "$HF_TOKEN" \
  --local-dir "$LORA_DIR"

# Stable name the API loads (FLUX_NSFW_LORA), independent of the source filename.
if [ "$LORA_FILE" != "lora.safetensors" ]; then
  ln -sf "$LORA_FILE" "$LORA_DIR/lora.safetensors"
fi
echo "[flux-lora] done → $LORA_DIR/lora.safetensors"
