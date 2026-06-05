#!/usr/bin/env bash
# Downloads PERO OCR models for historical Czech/German/Latin handwriting.
# Requires: CACHE_OCR set in .env (absolute host path for model storage).
set -euo pipefail

: "${CACHE_OCR:?CACHE_OCR must be set in .env}"

PERO_BASE="https://nextcloud.fit.vutbr.cz/s/pero-ocr-models/download?path=%2F&files="

# Models relevant for Austro-Hungarian matriky (1790–1900):
#   pero-czech-kurrent — German Kurrent + Latin + Czech, 19th century church records
#   pero-czech-print   — fallback for printed/semi-printed entries
declare -A MODELS=(
  ["pero-czech-kurrent"]="OCR_Printed_Kurrent_Czech_DCGM.zip"
  ["pero-czech-print"]="OCR_Printed_Czech_DCGM.zip"
)

mkdir -p "${CACHE_OCR}"

for name in "${!MODELS[@]}"; do
  archive="${MODELS[$name]}"
  dest="${CACHE_OCR}/${name}"

  if [[ -d "$dest" ]]; then
    echo "[pero] $name already present, skipping"
    continue
  fi

  echo "[pero] Downloading $name ..."
  tmp=$(mktemp -d)
  curl -fL --progress-bar "${PERO_BASE}${archive}" -o "${tmp}/${archive}"
  unzip -q "${tmp}/${archive}" -d "${tmp}/extracted"
  mv "${tmp}/extracted/"* "$dest"
  rm -rf "$tmp"
  echo "[pero] $name → ${dest}"
done

echo "[pero] Done. Models in ${CACHE_OCR}"
