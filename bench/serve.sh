#!/usr/bin/env bash
# Jednorázové vLLM kontejnery pro modely, které nemají službu (PLAN-model-bench.md §4).
# Spouštět z ~/deploy/AiStack. Každý projde mem-admit (rezerva 2 GiB), jinak nenaběhne.
#
#   bench/serve.sh gemma    # Gemma-4-31B-IT-NVFP4, ~35 GiB, den (vedle ComfyUI)
#   bench/serve.sh llama33  # Llama-3.3-70B-Instruct fp8, ~85 GiB, jen noc (director dolů)
#   bench/serve.sh nano     # Nemotron-3-Nano-30B, 0.22 (~27 GiB) — compose 0.15 od vLLM 0.21 nestačí na KV cache
#   bench/serve.sh stop gemma|llama33|nano
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
  nano)
    # Síťový alias swarm-nano: LiteLLM alias bench-nano míří na http://swarm-nano:8000.
    # compose swarm-nano má util 0.15 → „No available memory for the cache blocks“
    # (30. 9.: vLLM 0.21+ počítá CUDA grafy do util). Kontejner swarm-nano musí stát.
    docker stop swarm-nano >/dev/null 2>&1 || true
    scripts/mem-admit.sh bench-nano 0.22
    docker run -d --name bench-nano --network ai --network-alias swarm-nano --gpus all --ipc host \
      -e HF_HUB_OFFLINE=1 -e VLLM_USE_FLASHINFER_MOE_FP4=1 -e VLLM_FLASHINFER_MOE_BACKEND=throughput \
      -v "$CACHE_SWARM_NANO:/root/.cache/huggingface/hub" -v "$PWD/deploy/parsers:/parsers:ro" \
      "vllm/vllm-openai:${SWARM_VLLM_VERSION}" "$HF_MODEL_SWARM_NANO" \
      --served-model-name swarm-nano --gpu-memory-utilization 0.22 --max-model-len 32768 --max-num-seqs 4 \
      --enable-auto-tool-choice --tool-call-parser qwen3_coder \
      --reasoning-parser-plugin /parsers/nano_v3_reasoning_parser.py --reasoning-parser nano_v3 --trust-remote-code
    ;;
  stop)
    docker rm -f "bench-${2:?gemma|llama33|nano}"
    ;;
  *) sed -n 2,8p "$0"; exit 2 ;;
esac
echo "čekám na /v1/models (první start kompiluje, může trvat 10+ min): docker logs -f bench-${1}"
