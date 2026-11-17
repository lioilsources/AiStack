# Porty AiStacku

Přesunuto z `CLAUDE.md`. Zdroj: `ports:` v `deploy/*.yaml`, `spark/monitoring/systemd/`,
`services/spark-broker/deploy/config.example.yaml`. Bind je adresa na hostu SPARKu.
Při změně portu oprav i sdílený registr portů (DevConventions).

## Publikované (Docker)

| port | bind | kontejner | poznámka |
|------|------|-----------|---------|
| 8080 | 0.0.0.0 | gateway (`ai-gateway`) | veřejný přes Cloudflare, před LiteLLM |
| 8091 | 0.0.0.0 | gen-queue (`ai-gen-queue`) | FLUX NIM fronta + image API; cloudflared `/nim/*`, LAN pro NAS reroll |
| 8093 | 0.0.0.0 | audio | orchestrátor hudby, SFX a TTS, přes gateway `/v1/audio/*`, LAN pro Kirian |
| 8081 | 0.0.0.0 | swarm-evaluator | profil `evaluator` |
| 8000 | 127.0.0.1 | lab | NIM |
| 8001 | 127.0.0.1 | dev | NIM |
| 8002 | 127.0.0.1 | fallback | vLLM |
| 8003 | 127.0.0.1 | ocr-api | NIM |
| 8004 | 127.0.0.1 | translate | NIM |
| 8005 | 127.0.0.1 | swarm-embed | vLLM, profil `embed` |
| 8010 | 127.0.0.1 | swarm-nano | vLLM |
| 8011 | 127.0.0.1 | swarm-coder | vLLM (NGC image) |
| 8012 | 127.0.0.1 | swarm-director | vLLM, profil `director` |
| 8013 | 127.0.0.1 | swarm-rag | vLLM, profil `rag` |
| 8014 | 127.0.0.1 | swarm-coder-nim | NIM, profil `nim-coder` |
| 8020 | 127.0.0.1 | tune-builder | vLLM |
| 8021 | 127.0.0.1 | tune-validator | NIM |
| 8030 | 127.0.0.1 | flux-schnell / flux-kontext / flux-dev | NIM, vzájemně výlučné; broker doplněk `flux-schnell` |
| 8040 | 127.0.0.1 | qwen36-agent | vLLM, profil brokera `llm`, LiteLLM `openclaw-default` |
| 8041 | 127.0.0.1 | vl | vLLM Qwen2.5-VL-7B, on-demand (`make up-vl`, broker doplněk `vl`), LiteLLM `vl` |
| 8042 | 127.0.0.1 | asr | vLLM Whisper large-v3-turbo, on-demand (`make up-asr`, broker doplněk `asr`), LiteLLM `asr` |
| 8094 | 127.0.0.1 | audio-music | ACE-Step 1.5 REST API, broker doplněk |
| 8095 | 127.0.0.1 | audio-sfx | MOSS-SoundEffect / Stable Audio Open wrapper, broker doplněk |
| 8102 | 127.0.0.1 | audio-tts | Kokoro + Piper na CPU, compose profil `tts` |
| 8103 | 127.0.0.1 | audio-tts-xtts | XTTS-v2, GPU, nekomerční, compose profil `tts-gpu` |
| 8104 | 127.0.0.1 | audio-tts-chatterbox | Chatterbox Multilingual, GPU, compose profil `tts-gpu` |
| 4001 | 127.0.0.1 | swarm-litellm | SwarmBattle gateway |

## Interní (jen síť `ai`, na host nepublikované)

| port | kontejner | poznámka |
|------|-----------|---------|
| 4000 | litellm | hlavní LiteLLM; z hostu jdi přes gateway :8080 |
| 8090 | controller (`ai-controller`) | Go, `services/controller-manager` |

## Mimo Docker a mimo tento compose

| port | bind | služba | poznámka |
|------|------|--------|---------|
| 8100 | 0.0.0.0 | spark-broker | host-native (user systemd), fronta úloh SPARKu |
| 8188 | LAN | ComfyUI | host-native, profil brokera `comfy` |
| 8101 | 0.0.0.0 | dabing-separate | Demucs, compose v repu CeskyDabing, broker doplněk `dabing-separate` |
| 3000 | 0.0.0.0 | mon-grafana | user systemd, dashboard SPARK + JODA (`spark/monitoring/`) |
| 9090 | 127.0.0.1 | mon-prometheus | user systemd |
| 9835 | 127.0.0.1 | mon-gpu-exporter | user systemd, nvidia-smi metriky |

`deploy/docker-compose.swarm.nas.yaml` (swarm-chromadb, :8006 na 0.0.0.0) je pro NAS, ne SPARK.
