# OPTIMIZE-FLUX — kvantizace image-api na DGX Spark (GB10)

> Stav: **rozhodnutí odloženo**, vrátit se později. Aktuálně image-api jede **bf16**
> (FLUX.1-dev + NSFW LoRA přepínatelná, Qwen-Image-Edit). Tento dokument shrnuje
> možnosti úspory paměti a jejich rizika, ať se dá rychle navázat.

## Kontext / proč to řešíme

- HW: **DGX Spark, GB10 Grace Blackwell**, 128 GB unified LPDDR5X (sdílí CPU+GPU),
  aarch64, Ubuntu 24.04, CUDA 13, GPU compute capability **sm_121**.
- Paměťová zátěž image-api v bf16: **FLUX ~24 GB + Qwen-Image-Edit ~54 GB ≈ 78 GB**.
  Vejde se do 128 GB, ale těsně — a vedle běží LLM stack (lab gpt-oss-120b + dev
  Gemma-4-31B NVFP4) a OCR, takže společný pool je pod tlakem.
- Cíl: **uvolnit paměť** (a ideálně i zrychlit), **bez ztráty runtime přepínání
  SFW/NSFW** (NSFW LoRA jako živý adaptér).
- Ověřeno v session: jeden FLUX job (n=1, 14 kroků) ≈ 30 s, GPU 96 %, ~84 W,
  48→74 °C. Tj. GPU se zapřahá normálně, jen krátce.

## Relevantní místa v kódu

- `services/image-api/main.py`
  - `_load_flux()` — má větev `FLUX_FP8=1` přes `optimum-quanto`
    (`quantize()` + **`freeze()`**). Pozor: `freeze()` zapeče váhy → **na takový
    transformer už nejde přilepit LoRA** (proto fp8 + živá LoRA nejdou současně).
  - `_attach_flux_lora()` — načte NSFW LoRA jako peft adaptér `"nsfw"`, defaultně
    `disable_lora()`.
  - `_set_flux_variant()` — per-job toggle (bezpečné, GPU worker je single-thread).
- `services/image-api/Dockerfile` — `pip uninstall -y torchao` (base image má
  torchao 0.12, peft>=0.19 hází ImportError na <0.16). **Pozn.: pokud půjdeme
  torchao/QLoRA cestou, tohle se obrátí na upgrade torchao>=0.16.**
- `services/image-api/requirements.txt` — `peft`, `optimum-quanto`, diffusers z git.
- `deploy/docker-compose.image.yaml` — env `FLUX_FP8`, `LOAD_FLUX*`, `FLUX_NSFW_LORA*`.

## Možnosti

### A) FP8 teď — doporučeno (nízké riziko, in-tree)
- **Qwen-Image fp8** přes quanto: ~54 → ~27 GB, žádný LoRA konflikt (Qwen LoRA nemá).
  Zatím není fp8 větev pro Qwen v `_load_qwen_image()` — doplnit obdobně jako u FLUXu.
- **FLUX**: dvě dílčí volby
  - **bf16** (necháme) → **přepínání SFW/NSFW zůstává** (preferováno).
  - **fp8 + fused LoRA** (`load_lora_weights` → `fuse_lora()` → quantize/freeze)
    → ušetří i na FLUXu, ale **ztratíš runtime přepínání** (NSFW zapečená).
- Riziko: nízké. Quanto fp8 už v kódu je. Spustitelné hned.
- Zisk: ~27 GB (Qwen) [+ ~12 GB když fp8 i FLUX].

### B) NVFP4 přes nunchaku / SVDQuant — kvalitativní vítěz, ale dnes BLOCKED
- Outliery do 16-bit low-rank větve → 4-bit drží ~16-bit kvalitu. **Umí off-the-shelf
  LoRA bez rekvantizace** → zachoval by náš SFW/NSFW přepínač.
- Úspora ~3,5× vs FP16 (FLUX ~7 GB, Qwen ~15 GB) + ~3× rychlost na Blackwell.
- 🔴 **Blocker na GB10/aarch64**: prebuilt wheel jen x86_64; build ze zdroje na
  sm_121 padá (`No SM targets found`). Issue nunchaku **#799** k 11/2025 **open**.
- Riziko: vysoké → spike s reálnou šancí, že to nerozchodíme. **Fallback: FP8.**
- **TODO až se vrátíme: zkontrolovat issue #799 / nové aarch64 wheels.**
  https://github.com/nunchaku-ai/nunchaku/issues/799

### C) NVFP4 přes TensorRT-Model-Optimizer (modelopt) — oficiální, ale ztratí feature
- Reálný zisk vyžaduje **build TensorRT enginu** (fake-quant v PyTorchu jen simuluje,
  nešetří). Existuje „DGX Spark NVFP4" stránka NVIDIA.
- 🔴 Engine je **statický** → **runtime přepínání LoRA ztratíš** (jiná LoRA = re-export).
- 🔴 Toolchain aarch64/sm_121/cu13 nezralý (nightly wheels, rebuildy).
- Riziko: vysoké + ztráta hlavního featuru. Spíš ne.

### D) QLoRA styl (torchao int8/fp8 + živý peft adaptér)
- Kvantizovaný FLUX + LoRA jako živý adaptér (peft umí injektovat na torchao/bnb
  vrstvy) → přepínání zůstane i s kvantizací.
- Vyžaduje **torchao>=0.16** (obrátit Dockerfile fix na upgrade), bleeding-edge na
  aarch64/Blackwell — nutno ověřit, nemusí fungovat.
- Mezistupeň mezi A a B.

## Doporučení

1. **Teď: A) FP8** — fp8 na Qwen (největší jednotlivá výhra, ~27 GB), FLUX nechat
   bf16 (přepínání SFW/NSFW zůstává). Nízké riziko, žádný nový toolchain.
2. **Později: B) NVFP4 přes nunchaku**, až budou GB10/aarch64 wheels — jediná cesta
   s FP4 kvalitou/rychlostí *a* zachovaným přepínáním LoRA. Hlídat issue #799.
3. C/D jen pokud A nestačí na paměť a jsme ochotni obětovat přepínání / čas na spike.

## Otevřené otázky (na rozmyšlenou)

- Běží image-api souběžně s LLM+OCR? (kolik paměti reálně potřebujeme uvolnit)
- Je přepínání SFW/NSFW must-have? (pokud ano → C odpadá)
- FP4 láká kvůli paměti, rychlosti, nebo „headline formátu"? (mění prioritu)
- Tolerance k bleeding-edge spike, co možná nepůjde rozchodit?

## Verifikace (až se bude implementovat)

- Rebuild: `make up-image` (rebuild kvůli změnám deps), pak
  `curl -s localhost:8002/health` → `flux_*`/`qwen_image_loaded`.
- Paměť: `nvidia-smi --query-gpu=memory.used --format=csv` před/po (unified — sleduj
  i `free -g`).
- Zátěž/rychlost: poslat SFW request a vzorkovat
  `nvidia-smi dmon -s put` během generace; porovnat čas/krok vs bf16 baseline (~30 s).
- Kvalita: vizuálně porovnat výstup fixního promptu+seedu bf16 vs kvantizace
  (pozor na detaily/text/ruce).
- LoRA: ověřit, že po kvantizaci `flux_nsfw_lora_loaded=true` a že `nsfw:true/false`
  dává viditelně různý výstup (pokud zvolená cesta přepínání zachovává).

## Odkazy

- SVDQuant+NVFP4 (MIT HAN Lab): https://hanlab.mit.edu/blog/svdquant-nvfp4
- nunchaku: https://github.com/nunchaku-tech/nunchaku — GB10 issue #799
- NVIDIA Model-Optimizer (diffusers): https://github.com/NVIDIA/TensorRT-Model-Optimizer
- NVFP4 úvod: https://developer.nvidia.com/blog/introducing-nvfp4-for-efficient-and-accurate-low-precision-inference/
