# Licence audio modelů

Kirian jde na Steam, takže **do produkce smí jen model s komerční licencí**.
Tenhle soubor je shrnutí; strojově čitelná verze je `GET /v1/audio/models`
(zdroj obojího je `app/catalog.py`).

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

Seznam vrací i `GET /v1/audio/models/excluded`, aby se stejná otázka nemusela
řešit podruhé.
