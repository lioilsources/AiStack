#!/usr/bin/env bash
# BAAI/bge-m3 (~4.5 GB on disk incl. ONNX) — Shopify embeddings on CPU (deploy/docker-compose.shop-embed.yaml)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${CACHE_SHOP_EMBED:-$ROOT/cache/shop-embed}"

mkdir -p "$CACHE_DIR"
echo "[shop-embed] downloading BAAI/bge-m3 → $CACHE_DIR/bge-m3"
# TEI picks its CPU backend itself: ONNX (onnx/) or candle (pytorch_model.bin).
# No imgs/, colbert or sparse heads; ~4.5 GB on disk.
hf download BAAI/bge-m3 \
  ${HF_TOKEN:+--token "$HF_TOKEN"} \
  --local-dir "$CACHE_DIR/bge-m3" \
  --include "*.json" "pytorch_model.bin" "sentencepiece.bpe.model" "1_Pooling/*" "onnx/*"
echo "[shop-embed] done"
