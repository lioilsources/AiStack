# Licence audio modelů

Kirian jde na Steam a MemeShorts budou monetizované, takže **do produkce smí
jen model s komerční licencí**. Tenhle soubor je shrnutí; strojově čitelná
verze je `GET /v1/audio/models` (TTS hlasy `GET /v1/audio/voices`), zdroj
obojího je `app/catalog.py` a `app/tts/presets.py`.

Licenční soubor každého modelu leží po stažení vedle vah
(`$AUDIO_MODELS_PATH/<model>/LICENSE*`). Do `THIRD_PARTY_LICENSES` hry patří
licence toho modelu, kterým assety opravdu vznikly — ne celý seznam.

## V produkci

| Model | Repo | Licence | Komerčně | Poznámka |
|---|---|---|---|---|
| ACE-Step 1.5 (turbo) | `ACE-Step/Ace-Step1.5` | MIT | ano, bez omezení | Model card výslovně říká, že výstupy jdou použít komerčně; trénováno na licencovaných, royalty-free a syntetických datech. |
| MOSS-SoundEffect v2.0 | `OpenMOSS-Team/MOSS-SoundEffect-v2.0` | Apache-2.0 | ano, bez omezení | 48 kHz, DiT + flow matching, až 30 s. |

## Záloha / alternativy

| Model | Repo | Licence | Komerčně | Poznámka |
|---|---|---|---|---|
| ACE-Step v1 3.5B | `ACE-Step/ACE-Step-v1-3.5B` | Apache-2.0 | ano, bez omezení | Fallback, kdyby v1.5 nejela na sm_121. Jiná architektura — `audio-music` ji neumí načíst, chtěla by vlastní image. |
| Stable Audio Open 1.0 | `stabilityai/stable-audio-open-1.0` | Stability AI Community | **jen do 1 M USD ročního obratu** | Gated repo — nutné odsouhlasit licenci na HF. Nad limit je potřeba enterprise licence. Layout diffusers, wrapper ji umí. |
| Stable Audio Open Small | `stabilityai/stable-audio-open-small` | Stability AI Community | **jen do 1 M USD ročního obratu** | Totéž, ARM-optimalizovaná varianta. Veze jen `.ckpt` ve formátu stable-audio-tools, ne diffusers — wrapper ji zatím neumí. |

Modely, které katalog vede, ale žádný runtime neobsluhuje, hlásí
`/v1/audio/models` jako `available: false` s vysvětlením v `detail` — aby job
nespadl až v generaci.

Apache-2.0 MOSS je proto lepší primární volba než Stable Audio Open: stejná
kategorie modelu, ale bez stropu na obrat a bez gatingu.

## Vyřazeno kvůli licenci vah

| Model | Důvod |
|---|---|
| Meta MusicGen | váhy CC-BY-NC-4.0 — nekomerční |
| Meta AudioGen | váhy CC-BY-NC-4.0 — nekomerční |
| AudioLDM2 | nekomerční licence |
| TangoFlux | nekomerční licence |
| MMAudio | kód MIT, ale **váhy CC-BY-NC-4.0** — nekomerční (plán je vedl jako „ověřit"; ověřeno, nejde použít) |
| Piper `en_US-lessac-*` | dataset Blizzard 2013 jen pro výzkum, komerční užití pro syntézu řeči výslovně zakázané |
| OpenVoice v1 | CC-BY-NC-4.0 (v2 je MIT, ale bez češtiny) |

Seznam vrací i `GET /v1/audio/models/excluded`, aby se stejná otázka nemusela
řešit podruhé.

## TTS (řeč) — ověřeno 2026-10-02

Zdroje: model card, `LICENSE*` a `MODEL_CARD` přímo na HF (odkazy v katalogu).
`commercial=false` u TTS znamená **nekomerční nebo neověřené** — `/v1/audio/tts`
takový model bez výslovného `commercial_only=false` odmítne (403) a filtr
`?commercial=true` ho z `/v1/audio/models` i `/v1/audio/voices` vyřadí.

| Model | Repo | Licence | Komerčně | Čeština | Poznámka |
|---|---|---|---|---|---|
| Kokoro-82M | `hexgrad/Kokoro-82M` (ONNX: `onnx-community/Kokoro-82M-v1.0-ONNX`) | Apache-2.0 (váhy i hlasy) | **ano** | **ne** | 54 hlasů (en-us, en-gb, es, fr, hi, it, ja, pt-br, zh). Trénováno na public domain, Apache/MIT, CC BY datech a **syntetickém audiu z komerčních TTS** (model card) — licence vah to nemění, ale je dobré to vědět. |
| Piper `cs_CZ-kasandra-medium` | `rhasspy/piper-voices` | **CC BY 4.0** | **ano, s uvedením autora** | ano | Autor Ondřej Šimek, vlastní nahrávky („I own the rights to the dataset"). Ke každému výstupu: *Hlas Kasandra © Ondřej Šimek, CC BY 4.0* (katalog to vrací v `attribution`, job v manifestu). Jediný komerčně čistý český hlas. Ženský. |
| Piper `cs_CZ-jirka-medium` / `-low` | `rhasspy/piper-voices` | repo MIT, dataset CC0 | **neověřené → ne** | ano | MODEL_CARD: *Finetuned from U.S. English lessac voice*. Lessac stojí na datasetu Blizzard 2013, jehož licence výslovně vylučuje „development, marketing, commercialisation … of voice synthesis products or services". Jestli se to přenáší na dotrénovaný hlas, je právní otázka — do vyjasnění jen pokusy. Mužský. |
| XTTS-v2 | `coqui/XTTS-v2` | Coqui Public Model License 1.0.0 | **NE** | ano | „This license allows only non-commercial use of a machine learning model and its outputs." Monetizovaný short je komerční. Coqui v lednu 2024 skončil, komerční licenci už není od koho koupit. Ke každé kopii výstupu musí jít text licence nebo URL `https://coqui.ai/cpml.txt`. Klonování hlasu, 17 jazyků. |
| Chatterbox Multilingual (V3/V2) | `ResembleAI/chatterbox` | MIT | **ano** | **ne** | 23 jazyků (ar, da, de, el, en, es, fi, fr, he, hi, it, ja, ko, ms, nl, no, pl, pt, ru, sv, sw, tr, zh), klonování hlasu. **Každý výstup nese neslyšitelný vodoznak Resemble Perth** (přežije MP3 i střih) — pro AI-labeling (EU AI Act) spíš výhoda. |
| Chatterbox čeština (`chatterbox-cs`) | `Thomcles/Chatterbox-TTS-Czech` (gated) | váhy CC0-1.0 | **neověřené → ne** | ano | Komunitní T3 nad Chatterbox Multilingual. Trénováno na `Thomcles/YodaLingua-Czech` (CC BY 4.0, ale gated s podmínkou „use it solely for training and experimentation" a bez popsaného původu nahrávek). |

Kód runtime: piper-tts 1.8 (`OHF-Voice/piper1-gpl`) je **GPL-3.0**, kokoro-onnx
používá espeak-ng (GPL-3.0), coqui-tts je MPL-2.0. GPL se týká distribuce kódu,
ne vygenerovaného audia — kontejnery se jen provozují, nikomu se nedávají.

**Klonovaný hlas** (XTTS, Chatterbox) má i druhou licenci: práva k hlasu
člověka na referenčním vzorku. Upload (`POST /v1/audio/voices`) proto chce
`rights` (own | consented | licensed | synthetic) a `source`; obojí jde do
manifestu každého jobu. Hlas, ke kterému nemáš svolení, se neklonuje.

### Co z toho plyne pro MemeShorts

- **EN:** Kokoro (preset, Apache) nebo Chatterbox (klon hlasu postavy, MIT +
  vodoznak). Obojí komerčně.
- **CZ komerčně:** jen Piper Kasandra (jeden ženský hlas, uvést autora).
  „Jeden hlas na postavu" v češtině komerčně zatím nejde — čeština s klonem je
  jen XTTS-v2 (nekomerční) a chatterbox-cs (neověřený).
- Cesty, jak to rozšířit: vyjasnit licenci chatterbox-cs s autorem; nebo
  natrénovat vlastní Piper hlas od nuly (ne z lessac) na vlastních nahrávkách.
