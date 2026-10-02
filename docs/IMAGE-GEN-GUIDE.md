# Generování obrázků na SPARKu — průvodce pro projekty

Stav 2026-10-02. Pro sessions, které plánují grafiku (Mutants, Kittens, BioDefenseRogue, …).
„Neověřeno“ = nezměřené, v plánu tak označit. Okna a profily: `PLAN-spark-scheduler.md` §3 (jediný zdroj
časů). Dávky ohlásit session **Director** předem (čas, počet).

## 1. Co běží a kdy

| Backend | Okno | Na co |
|---|---|---|
| **flux-schnell NIM** přes gen-queue | comfy 07–13, llm 19–01 | tier 0, txt2img, 4 kroky, rychlý |
| **ComfyUI** (`http://192.168.88.66:8188`, LAN bez auth) | jen comfy 07–13 (primárně experimenty uživatele) | vše ostatní: img2img, ControlNet, IP-Adapter, inpaint, upscale, výřez, video |
| nic obrázkového | director 13–19 a 01–07 | director (91 GiB) se s ničím jiným nevejde |

## 2. Modely v ComfyUI

- **flux**: dev, schnell, Kontext (edit / img2img z reference), Fill (inpaint)
- **SDXL**: base 1.0, Juggernaut XL v9 (+ Lightning), RealVis 5, CyberRealistic; Illustrious 2.0, NoobAI, Animagine 4, Hassaku, Pony v6
- **ControlNet**: SDXL union promax (xinsir), SDXL openpose, flux union pro 2, flux depth v3, InstantID
- **IP-Adapter**: Plus SDXL (style/obsah reference), FaceID Plus v2
- **LoRA**: Pixar styl pro SDXL / Illustrious / Pony / Flux a další
- **výřez pozadí**: ComfyUI-RMBG (BiRefNet / RMBG) — nainstalované, neměřené; **LayerDiffuse (přímá alfa) není**
- **upscale**: 4x-UltraSharp
- **video**: Wan 2.2 (ti2v 5B ~25 min / 121 snímků; i2v, t2v, VACE, control 14B), LTX 2.3, AnimateDiff, frame interpolation
- **3D**: Trellis2, Hunyuan3D; **audio v ComfyUI**: MMAudio
- **Qwen-Image není.** Bezešvé dlaždicování (tileable) nemá vlastní node — neověřeno; obejít offsetem o půl
  dlaždice + inpaint švu (flux Fill nebo inpaint nodes).

## 3. Jak zadat job (sám, přes HTTP)

Kontejnery nikdy nestartovat ani nezastavovat. Výstupy si stáhnout do vlastního repa (gitignored drafty,
do assets až vybrané) — sdílený svazek není.

**flux-schnell NIM** (gen-queue):

```bash
curl -s -X POST http://192.168.88.66:8091/nim/flux-schnell/v1/infer \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"…","width":1024,"height":1024,"seed":42,"steps":4}'   # → 202 {"id":"…","queue_position":N}
curl -s http://192.168.88.66:8091/nim/flux-schnell/jobs/<id>           # poll: queued | running | done | error
curl -s -o out.jpg http://192.168.88.66:8091/nim/flux-schnell/jobs/<id>/result   # bajty jsou JPEG
```

- Výsledek se drží 1 h, pak zmizí i stav jobu. Fronta je jedna a sdílená: **čekat na `done` minuty, ne
  sekundy** (storyteller 1. 10.: klient s 15s timeoutem posílal duplikáty). Souběžnost 2 na projekt.
- Seed < 2³² (NIM vrací 422). Obdélníkové rozměry: násobky 64, neověřeno.
- flux-schnell **ignoruje „no text“** a rád dokreslí falešné popisky (storyteller: ~40 % u „picture book“
  promptů). Pomáhá formulace bez čeho popisovat: portrét na plochém pozadí, „3D film still“, žádné
  „book/poster/label“. Kontrola OCR: storyteller `tools/find-lettering.swift`.

**ComfyUI**: `POST /prompt` s workflow v API formátu, `GET /history/{id}`, `GET /view?filename=…`,
upload `POST /upload/image`. Vzory klientů: storyteller `internal/comfy`, Kiran `pipeline`,
Ol1nLLM `tools/lab`. Workflow flux-schnell txt2img / img2img: Ol1nLLM větev `feat/comfy-flux-schnell`.

## 4. Rychlost (měřeno, kde neuvedeno jinak)

| Co | Čas na obrázek |
|---|---|
| flux-schnell NIM 1024² | ~2,7 s sám, ~5,5 s vedle LLM; při frontě jiných dávek čekání |
| flux-dev ComfyUI 20 kroků | ~45 s |
| SDXL + ControlNet / IP-Adapter | 10–20 s (neověřeno) |
| Wan 2.2 ti2v 5B, 121 snímků | ~25 min, bez checkpointu — nespouštět < 40 min před koncem okna |

Rozumně 3–4 varianty na prompt.

## 5. Jednotný styl a postava (z labu StoryTelleru, `storyteller/STORYTELLER_CHARACTER_MODELS.md`)

- **Prompt + seed drží styl, ne postavu.** Stejný prompt a seed v jiném modelu = jiná postava.
- **Postavu drží img2img z reference** (flux Kontext, SDXL img2img): 85–100 % nad branou DINO 0,80.
- **Depth ControlNet 0,4** drží siluetu a pouští styl naplno (flux-schnell: 65 % nad branou).
- U flux-schnell v ComfyUI jde denoise se 4 kroky jen skokově: 0,85–1,0 = plné přemalování.
- Doporučení: pevný stylový blok v promptu + 1–3 schválené reference (IP-Adapter / Kontext / img2img) +
  pevný seed na asset; pro skládané díly šablona siluety s kotevními body přes ControlNet.
- Trénink vlastní LoRA: neověřeno (zeptat se session FineTune).
- Paleta: pipeline ji přesně nedodrží — kvantizovat v postprocessu (PIL `quantize` s vlastní paletou).
- Průhlednost: generovat na plochém jednobarevném pozadí a vyříznout (ComfyUI-RMBG, nebo vlastní
  klíčování — Kiran má adaptivní odmazávač, práh 35).

## 6. Licence (neověřeno — ověřit u každého modelu před vydáním)

| Model | Licence | Komerční appka |
|---|---|---|
| flux-schnell | Apache-2.0 | ano |
| SDXL base 1.0 | CreativeML OpenRAIL++-M | ano (s use-based omezeními) |
| flux-dev, Kontext, Fill, flux ControlNety (trénované nad dev) | FLUX.1 [dev] Non-Commercial | ověřit, nejspíš ne |
| Juggernaut, RealVis, CyberRealistic, Illustrious, NoobAI, Animagine, Pony, LoRA z Civitai | vlastní licence | ověřit každou |
| audio: ACE-Step (MIT), MOSS-SoundEffect (Apache-2.0) | | ano |

Nejbezpečnější cesta pro App Store / Play / Steam: flux-schnell, případně SDXL base + xinsir ControlNet.

## 7. Zvuk

AiStack `/v1/audio` přes gateway :8080 (`POST /v1/audio/sfx`, `/v1/audio/music` → job). Modely běží jen
v okně comfy 07–13. Detaily v `CLAUDE.md`, sekce Audio.
