#!/usr/bin/env bash
# Profil gptoss pro spark-broker: všechno velké dolů, gpt-oss-120b nahoru jako
# LiteLLM "lab". Stejný vzor jako AiStack bench/serve.sh (síť ai, HF offline,
# mem-admit). Lokální o200k tiktoken je povinný (offline start jinak padá).
set -euo pipefail
AISTACK="${AISTACK:-$HOME/deploy/AiStack}"
SPARK_DIR="${SPARK_DIR:-$HOME/deploy/WorldLibraryProject/deploy/spark}"
systemctl --user stop comfyui || true
docker stop qwen36-agent flux-schnell audio-music audio-sfx audio-tts-xtts audio-tts-chatterbox >/dev/null 2>&1 || true
( cd "$AISTACK" && make down-swarm-director >/dev/null 2>&1 ) || true
docker rm -f bench-gemma >/dev/null 2>&1 || true
python3 "$SPARK_DIR/evict-model-cache.py" || true
sleep 10
cd "$AISTACK"
scripts/mem-admit.sh bench-gptoss 0.65
docker rm -f bench-gptoss >/dev/null 2>&1 || true
docker run -d --name bench-gptoss --network ai --network-alias lab --gpus all --ipc host \
  -e HF_HUB_OFFLINE=1 -v "$AISTACK/cache/lab/hub:/root/.cache/huggingface/hub:ro" \
  -e TIKTOKEN_ENCODINGS_BASE=/tiktoken -v "$AISTACK/cache/lab/tiktoken:/tiktoken:ro" \
  vllm/vllm-openai:v0.20.0 openai/gpt-oss-120b \
  --served-model-name lab --gpu-memory-utilization 0.65 --max-model-len 32768 --max-num-seqs "${GPTOSS_SEQS:-12}" >/dev/null
for i in $(seq 1 80); do
  docker logs bench-gptoss 2>&1 | grep -q "Application startup complete" && { echo "gpt-oss nahoře po $((i*15)) s"; exit 0; }
  [ "$(docker inspect -f '{{.State.Running}}' bench-gptoss)" = true ] || { docker logs --tail 30 bench-gptoss; exit 1; }
  sleep 15
done
echo "gpt-oss do 20 min nenaběhl"; docker logs --tail 30 bench-gptoss; exit 1
