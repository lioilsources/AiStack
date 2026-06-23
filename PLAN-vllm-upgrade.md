# Analýza: nvidia/vllm image po upgradu driveru na 595

## Kontext
Po upgradu driveru na 595 (`sudo apt install nvidia-driver-595` + reboot) je možné přepnout
modely na `nvcr.io/nvidia/vllm:${VLLM_VERSION}` pro TRT-LLM throughput.

---

## Audit servicí a kompatibility s nvidia image

| Service | Model | Kvant. | `vllm serve` v cmd? | nvidia/vllm bezpečné? | Benefit |
|---------|-------|--------|--------------------|-----------------------|---------|
| **lab** | gpt-oss-120b | ? | ✅ má | ✅ Ano (už tak nakonfig.) | maximální |
| **dev** | Gemma-4-31B-IT-NVFP4 | NVFP4 | ❌ chybí | ❌ **Blokováno**: nvidia 26.03 = vLLM 0.17.1, Gemma-4 vyžaduje 0.20+; čekat na 26.05+ | — |
| **translate** | Qwen3-32B-AWQ | AWQ | ❌ chybí | ⚠️ Ano, přidat `vllm serve`; AWQ míň benefituje | střední |
| **tune-builder** | Nemotron-Nano-30B-A3B-NVFP4 | NVFP4 | ❌ chybí | ⚠️ **Riziko**: custom `--reasoning-parser-plugin nano_v3`; ověřit jako swarm-nano | velký ale rizikový |
| **tune-validator** | Qwen2.5-VL-7B-Instruct | BF16 | ❌ chybí | ⚠️ Ano, ale VLM — menší benefit | malý |
| **swarm-nano** | Nemotron-Nano-30B NVFP4 | NVFP4 | ✅ má | ⚠️ **Riziko**: custom `--reasoning-parser nano_v3` plugin; ověřit kompatibilitu s 0.17.1 | velký ale rizikový |
| **swarm-director** | Nemotron-Super-120B NVFP4 | NVFP4 | ✅ má | ⚠️ **Riziko**: custom `--reasoning-parser nemotron_v3`; ověřit kompatibilitu s 0.17.1 | velký ale rizikový |
| **swarm-coder** | Phi-4 (dense) | BF16 | ✅ má | ✅ Bez rizika, menší benefit | malý |
| **swarm-rag** | Llama-3.1-Nemotron-8B | BF16 | ✅ má | ✅ Bez rizika, menší benefit | malý |

**Klíčová podmínka:** Community image (`vllm/vllm-openai`) má `vllm serve` jako entrypoint
→ funguje bez prefixu v command. Nvidia image potřebuje **`vllm serve` explicitně** v command.
Servisy bez tohoto prefixu by selhaly s nvidia image.

---

## Co implementovat po upgradu driveru

### Fáze 1 — Bezpečné přepnutí (translate, tune-validator) — ✅ HOTOVO 2026-06-20
### Dev blokován — Gemma-4 nepodporován v nvidia 26.03 (vLLM 0.17.1)

**`docker-compose.llm.yaml` — dev service:**
```yaml
image: nvcr.io/nvidia/vllm:${VLLM_VERSION}
command: >
  vllm serve ${HF_MODEL_DEV}    ← přidat "vllm serve"
  --served-model-name dev
  ...
environment:
  - VLLM_NVFP4_GEMM_BACKEND=marlin   ← přidat
```

**`docker-compose.translate.yaml`:**
```yaml
image: nvcr.io/nvidia/vllm:${VLLM_VERSION}
command: >
  vllm serve ${HF_MODEL_TRANSLATE}   ← přidat "vllm serve"
  ...
```

**`docker-compose.tune-image.yaml` — tune-builder + tune-validator:**
```yaml
image: nvcr.io/nvidia/vllm:${VLLM_VERSION}
command: >
  vllm serve ${HF_MODEL_TUNE_BUILDER}   ← přidat "vllm serve"
```

### Fáze 2 — Custom parsers (swarm-nano, swarm-director, tune-builder)

Servisy mají `vllm serve` v command ✓, ale používají custom reasoning parsery
(`nano_v3`, `nemotron_v3`) mountované jako `./parsers:/parsers:ro`.

**tune-builder přeřazen do Fáze 2** — má identickou konfiguraci jako swarm-nano
(`--reasoning-parser-plugin /parsers/nano_v3_reasoning_parser.py --reasoning-parser nano_v3`).

Ověřit: funguje `--reasoning-parser-plugin /parsers/nano_v3_reasoning_parser.py` s
nvidia image (vllm 26.03)? Pokud ano → přepnout swarm-nano, swarm-director, tune-builder.
Swarm-coder a swarm-rag jsou low-risk kdykoli (žádné custom parsery).

### Mezitím (před rebooten) — Krok 0: gemma4-cu130

Teď (bez rebootu): přepnout dev na `vllm/vllm-openai:gemma4-cu130` + `VLLM_NVFP4_GEMM_BACKEND=marlin`.

---

## Driver upgrade postup

```bash
sudo apt install nvidia-driver-595
sudo reboot
# Po rebootu:
nvidia-smi  # ověřit 595.71.05
```

Tato změna nevyžaduje reinstalaci CUDA ani přepínání image cache — jen nový driver.

---

## Ověření po přepnutí na nvidia image

```bash
docker logs ai-dev 2>&1 | grep -E "vLLM Version|TRT|marlin|NVFP4|error" | head -10
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llm-dev","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' \
  | jq '.model,.choices[0].message.content'
```
