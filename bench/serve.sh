#!/usr/bin/env bash
# Jednorázové vLLM kontejnery pro modely, které nemají službu (PLAN-model-bench.md §4).
# Spouštět z ~/deploy/AiStack. Každý projde mem-admit (rezerva 2 GiB), jinak nenaběhne.
#
#   bench/serve.sh gemma    # Gemma-4-31B-IT-NVFP4, ~35 GiB, den (vedle ComfyUI)
#   bench/serve.sh llama33  # Llama-3.3-70B-Instruct fp8, ~85 GiB, jen noc (director dolů)
#   bench/serve.sh stop gemma|llama33
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

case "${1:-}" in
  gemma)
    scripts/mem-admit.sh bench-gemma 0.30
    docker run -d --name bench-gemma --network ai --gpus all --ipc host \
      -e HF_HUB_OFFLINE=1 -v "$CACHE_DEV/hub:/root/.cache/huggingface/hub:ro" \
      vllm/vllm-openai:gemma4-cu130 nvidia/Gemma-4-31B-IT-NVFP4 \
      --served-model-name gemma --gpu-memory-utilization 0.30 --max-model-len 16384 --max-num-seqs 4 \
      --enable-auto-tool-choice --tool-call-parser gemma4 --trust-remote-code
    ;;
  llama33)
    snap=$(ls -d "$HOME"/.nim/cache/ngc/hub/models--nim--meta--llama-3.3-70b-instruct/snapshots/fp8-*tool-calling)
    blobs="$HOME/.nim/cache/ngc/hub/models--nim--meta--llama-3.3-70b-instruct/blobs"
    scripts/mem-admit.sh bench-llama33 0.70
    # snapshot = symlinky do ../../blobs → namontovat oba na stejné relativní místo
    docker run -d --name bench-llama33 --network ai --gpus all --ipc host \
      -v "$snap:/m/snapshots/s:ro" -v "$blobs:/m/blobs:ro" \
      "vllm/vllm-openai:${SWARM_VLLM_VERSION}" /m/snapshots/s \
      --served-model-name llama33 --quantization modelopt --gpu-memory-utilization 0.70 \
      --max-model-len 16384 --max-num-seqs 2 --enable-auto-tool-choice --tool-call-parser llama3_json
    ;;
  stop)
    docker rm -f "bench-${2:?gemma|llama33}"
    ;;
  *) sed -n 2,8p "$0"; exit 2 ;;
esac
echo "čekám na /v1/models (první start kompiluje, může trvat 10+ min): docker logs -f bench-${1}"
