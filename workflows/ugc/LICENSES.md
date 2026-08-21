# Licence img→3D backendů (UGC pipeline)

| Model | Licence | EU OK? | Poznámka |
|---|---|---|---|
| microsoft/TRELLIS.2-4B | MIT | ✔ | bez omezení, primární backend |
| stabilityai/stable-fast-3d | Stability AI Community License | ✔ do $1M ročního obratu | gated na HF (auto-approve s tokenem); komerční užití free pod $1M/rok, jinak enterprise licence |
| Tencent Hunyuan3D (všechny verze) | Tencent Community License | ✘ **territory exclusion EU** | NEPOUŽÍVAT — stejný důvod jako vyřazení HunyuanVideo |

Pravidlo: každý nový img→3D model před instalací zkontrolovat na EU výjimky
(Tencent modely je mají standardně).

Stav ARM64 (GB10, ověřeno 2026-08-21):
- TRELLIS.2: funkční přes ComfyUI-Trellis2 (sdpa + flex_gemm + nvdiffrast, bez
  flash_attn/spconv — nejsou potřeba). Texturovaný GLB ověřen.
- SF3D: FUNKČNÍ (ověřeno 2026-08-21 po odemčení gated repa): kabuto
  cleanplate → 20 028 faces, UV, PBR 1024² textura za **12 s** inference,
  peak 6,2 GB — ~20× rychlejší než TRELLIS (~4 min), hrubší geometrie.
  uv_unwrapper i texture_baker zkompilovány (`--no-build-isolation`);
  gpytoolbox a pynanoinstantmeshes na ARM64 nejdou postavit → lazy import
  patch v sf3d/models/mesh.py, quad/triangle remesh nedostupný (UGC dráha ho
  nepotřebuje, decimace běží na NAS). Do gen-queue zapojit jako
  `backend: sf3d` = standalone volání (není ComfyUI node) — TODO fáze 5b.
