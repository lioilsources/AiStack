#!/usr/bin/env bash
# OCR models for historical handwriting (matriky 1790+):
#   dh-unibe/trocr-kurrent      — 19th century German Kurrent, CER ~2.7 %
#   dh-unibe/trocr-kurrent-XVI-XVII — 16th–18th century Kurrent (pre-1800 docs)
#
# Layout segmentation (baseline detection) is provided by the kraken BLLA model
# bundled inside the kraken Python package — no separate download required.
set -euo pipefail

: "${CACHE_OCR:?CACHE_OCR must be set in .env}"
: "${HF_TOKEN:?HF_TOKEN must be set in .env}"

MODELS=(
  "dh-unibe/trocr-kurrent"
  "dh-unibe/trocr-kurrent-XVI-XVII"
)

mkdir -p "${CACHE_OCR}"

for MODEL_ID in "${MODELS[@]}"; do
  NAME="${MODEL_ID//\//__}"
  DEST="${CACHE_OCR}/${NAME}"
  if [[ -d "$DEST" ]]; then
    echo "[ocr] ${MODEL_ID} already present, skipping"
    continue
  fi
  echo "[ocr] downloading ${MODEL_ID} → ${DEST}"
  hf download "${MODEL_ID}" \
    --token "${HF_TOKEN}" \
    --local-dir "${DEST}"
  echo "[ocr] ${MODEL_ID} done"
done

echo "[ocr] All models in ${CACHE_OCR}"
