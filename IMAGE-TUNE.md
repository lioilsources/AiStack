# IMAGE-TUNE.md — model serving pro Card Forge

Adaptace specu BACKEND_DEPLOY.md na reálný stav AiStacku (DGX Spark GB10,
128 GB unified LPDDR5X, aarch64). Dvoufázový workflow: **train** (builder LLM +
validátor VLM + generátor) a **bulk** (jen generátor, paměť volná na batchování).

## Role a modely

| Role | Model | Kde běží | Kdy |
|------|-------|----------|-----|
| Builder (prompty, item listy) | Nemotron-3-Nano-30B-A3B-NVFP4 | `tune-builder` (vLLM) | jen train |
| Validátor (VLM) | Qwen2.5-VL-7B-Instruct | `tune-validator` (vLLM) | jen train |
| Generátor | FLUX.1-dev-kontext fp8 + Pony V6 XL | ComfyUI nativně na hostu (:8188) | train i bulk |
| Brána | gateway :8080 → litellm | hlavní stack (běží pořád) | train i bulk |
| Orchestrátor `forge` | externí klient na LAN | mimo stack | train i bulk |

Odchylky od specu (záměrné):
- **ComfyUI se nekontejnerizuje** — běží nativně na hostu, modely už má, tunel
  comfyui.ol1n.com funguje. Žádný risk s ARM64 imagem.
- **Builder není Qwen3-30B-A3B** — Nemotron Nano (MoE 30B/3.5B active) je NVFP4
  optimalizovaný pro Blackwell a už je stažený (`cache/swarm-nano`).
- **Validátor 7B místo 32B** — už stažený (`cache/qwen-vl`); upgrade na
  Qwen2.5-VL-32B-AWQ jen pokud 7B selhává na jemných vadách.
- **Žádné compose profily ani forge služba** — dvě fáze = dva Makefile targety,
  forge se připojuje z lokální sítě.

## Endpointy pro forge (z LAN)

```
LLM/VLM:  http://<spark>:8080/v1/chat/completions   (gateway → litellm)
          model: "builder"   — prompty/item listy (reasoning off by default,
                               zapnout: chat_template_kwargs.enable_thinking=true)
          model: "validator" — vision kontrola (image_url, max 4 obrázky/prompt)
ComfyUI:  http://<spark>:8188                        (přímo, host-native)
```

## Workflow fází

```bash
# FÁZE A — train (recept na 3–5 příkladech): stopne dev, spustí builder+validator
make up-tune-image

# FÁZE B — bulk (bruteforce bez kontroly): shodí builder+validator,
# paměť zůstane ComfyUI na batchování
make down-tune-image

# návrat běžného provozu (dev = Qwen3-32B překlad)
make up-llm
```

Load modelů trvá desítky sekund až minuty — healthchecky mají `start_period: 300s`,
stav: `docker ps` (čekat na `healthy`).

## Paměťový rozpočet (train, dev stopped)

```
ComfyUI host: FLUX kontext fp8 ~17 + Pony ~7 + CLIP/VAE ~6   ≈ 30 GB
tune-builder  Nemotron Nano NVFP4 + KV                        ≈ 18–22 GB
tune-validator Qwen2.5-VL-7B bf16 + KV + vision               ≈ 18–20 GB
──────────────────────────────────────────────────────────────
celkem                                                        ≈ 66–72 GB
```

Bulk: jen ComfyUI (~30 GB) → zbytek poolu na batch generování.

## Soubory

- `deploy/docker-compose.tune-image.yaml` — služby tune-builder, tune-validator
  (externí síť `ai`, porty 127.0.0.1:8020/8021 pro debug)
- `deploy/litellm_config.yaml` — routy `builder`, `validator` (502 když stack neběží — OK)
- `.env` — `HF_MODEL_TUNE_*`, `CACHE_TUNE_*`
- `Makefile` — `up-tune-image`, `down-tune-image`

Pozn.: po změně `litellm_config.yaml` restartovat jen službu litellm
(`docker compose -f deploy/docker-compose.llm.yaml restart litellm`), ne celý
llm stack — `up` by kvůli `depends_on` vyžadoval healthy `dev`.
