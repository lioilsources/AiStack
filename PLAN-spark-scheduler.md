# PLAN-spark-scheduler.md — plné využití SPARKu pro víc projektů

Stav 2026-10-01 odpoledne. Navazuje na `PLAN-model-bench.md` (naměřené modely §6a). Jediné místo, kde
jsou okna a profily SPARKu — plány projektů na ně odkazují jménem profilu, ne hodinami.

**Proč:** starý rozvrh (comfy 07–00, promo 00–01, rag 01–07) znal jen ComfyUI, PromoClowna a Knihovníka.
Ostatní projekty se pouštěly ručně mimo rozvrh a dvakrát to skončilo incidentem (29. 9. CPU render +
přehřátí, 30. 9. qwen36 chyběl v promo okně). SPARK přitom přes den často stojí: ComfyUI je nahoře,
ale nic nerenderuje (log ComfyUI 24.–30. 9.: 27. a 28. 9. ani jeden prompt, jinak dávky po 1–4 h).

---

## 1. Kdo co potřebuje

Stav aktivní / **cold**. Cold = projekt blokuje něco mimo SPARK (vstup uživatele, chybějící přístup,
review); rozvrh s ním nepočítá, dokud se neodblokuje (§6).

| # | Projekt | Stav | Co na SPARKu | Profil | Objem |
|---|---|---|---|---|---|
| 1 | **StoryTeller** | aktivní | a) RAG dávky (world hints, extract) na directoru; b) obrázky: tier 0 flux-schnell, tier 2 a lab v ComfyUI | director; comfy (+ flux-schnell) | world hints ~55 h directora; lab vlny C–E ~2 h; tier 2 až po ref2img |
| 2 | **ToyShaders** (MirrorBooth ShaderGen) | aktivní | LLM `openclaw-default` (qwen36, kód + vidění) přes LiteLLM | llm | 2–4 okna celkem, pak ~1 h 1–2× týdně |
| 3 | **Knihovník** | aktivní | `library-enrich` (enrich_chunks) na directoru; chat přes řetěz `translate → openclaw-default → swarm-director → fallback` | director; chat v každém okně | enrich_chunks velkých děl + enrich_works: další noci |
| 4 | **Právník** | aktivní | `law-chat` (e5, 1,3 GiB, pořád); chat přes řetěz jako Knihovník; agent `pravnik-agent` → Gemma-4 | llm; gemma na vyžádání | interaktivní; agent ~4–5 min/návrh |
| 5 | **Kindlify** | aktivní | `library-chapters` (enrich_chapters) na directoru | director | 12 062 kapitol ≈ 4–5 nocí při 4 workerech; zh.daodejing a zh.lunyu jsou v abecedě až na konci (§7) |
| 6 | PromoClown | aktivní (mimo frontu) | heartbeat přes qwen36; tributy v ComfyUI | llm; comfy | tributy ~5–8 min denně |
| 7 | Kirian | aktivní, nárazově | ComfyUI (flux plachty, Wan 2.2 video ~25 min/kus bez checkpointu), audio | comfy | týden: ~10 min obrázky + ~75 min video; video nespouštět < 40 min před koncem okna |
| 8 | MusicStudio (Ol1nLLM) | aktivní, nárazově | audio-music (vibe) | comfy | interaktivní |
| 9 | HandWrittenStickers | aktivní, nárazově | ComfyUI SDXL img2img + Canny (~20 jobů na start, pak občas) | comfy | minuty; dnes čeká na CF Access token |
| 10 | SwypeKids | aktivní, zatím bez SPARKu | v3.0 možná jednorázová dávka desítek obrázků nálepek | comfy / flux-schnell | 0 teď |
| 11 | FineTune (VL soudce) | aktivní, jednorázově | `vl` (Qwen2.5-VL-7B, ~20 GiB) na kalibraci F6, ~240 requestů | llm (vejde se vedle qwen36) | 15–30 min, spouští uživatel |
| – | **AiSwarmBattle** | **cold** | swarm-nano, swarm-embed, swarm-litellm, evaluator; fáze 3 celý Nemotron stack | – | evaluator čeká na review uživatele; fáze 1 a 3 nezačaté |
| 12 | Aukrofy | aktivní (od 2. 10., z cold) | LLM `openclaw-default` (qwen36), sdílí, nic vlastního | llm | E2E běhy 10–30 min, ~8 souběžně, pár týdně |

## 2. Profily

Profil = sada služeb, která se do paměti vejde **se zachováním pravidel z incidentů**: ComfyUI render nikdy vedle
LLM ≥ 30 GiB, kontejnery jen `docker stop` (nikdy `compose down`), před startem `mem-admit` + kontrola KV cache.
Základ (OS, LiteLLM, gateway, chroma, law-chat, library-chat, postiz, kontejnery bez GPU) ≈ 20 GiB.

| Profil | Nahoře | GiB | Slouží |
|---|---|---|---|
| **comfy** | ComfyUI (≤ 52) + flux-schnell NIM (17); audio (25) jen na vyžádání | ~90 | lab a tier 2 StoryTelleru, Kirian, Stickers, PromoClown tributy, MusicStudio |
| **llm** | qwen36 (36,5) + flux-schnell NIM (17) | ~75 | Právník chat, ToyShaders, Knihovník chat, PromoClown heartbeat, **tier 0 obrázky**; `vl` (20) na vyžádání |
| **gemma** (na vyžádání) | Gemma-4 (util 0,40 ≈ 49) + flux-schnell (17) | ~86 | agent Právníka místo qwen36 |
| **director** | swarm-director 0,75 (91) sám | ~111 | Knihovník enrich + Kindlify chapters + StoryTeller RAG |

- **Nano-30B, swarm-embed a swarm-litellm z profilu llm vypadly** — sloužily jen AiSwarmBattle (cold);
  ToyShaders jede na qwen36. Vrátí se s ním (Nano util 0,22 = 27 GiB se vedle qwen36 + flux-schnell vejde).
- **flux-schnell NIM v profilu llm** (nové): TensorRT engine si paměť alokuje celou při startu a nepadá na CPU,
  takže na něj pravidlo „ComfyUI vedle LLM“ nedopadá (ten incident byl torch v ComfyUI, který vidí jen MemFree).
  Tier 0 obrázky přes gen-queue (`/nim/flux-schnell`, 1–2 s/obrázek) jsou pak k dispozici v comfy i llm okně.
  Vedle directora ne — 25. 9. to shodilo celý stroj.
- **Audio v comfy jen na vyžádání** (od 2026-10-06): `docker start audio-music audio-sfx` nebo controller
  `/ctrl/activate?model=audio-music`; při přepnutí z comfy se zastaví. S audiem a flux-schnell naráz nezbývalo
  ComfyUI dost MemFree a po vystřídání velkých modelů počítalo na CPU (6. 10., prompt 493 s).
- **translate** (Qwen3-32B) z rozvrhu vypadl: qwen36 ho ve všem předčí (bench §6a); alias `translate` dál
  existuje a padá řetězem na model okna.

## 3. Denní rozvrh

### 3a. Fáze 1 (2026-10-01 dopoledne, nahrazeno 3b)

| Čas | Profil |
|---|---|
| 07–17 | comfy (tributy 16:30) |
| 17–01 | llm (heartbeat PromoClowna 17:05–00:55) |
| 01–07 | director |

### 3b. Platí od 2026-10-01 14:16: kratší comfy, director i přes den

ComfyUI nikdo nepotřebuje denně (§1: Kirian, Stickers, MusicStudio, lab — vše nárazově, minuty až hodiny týdně)
a tier 0 obrázky obslouží flux-schnell i v llm okně. Naopak director má frontu na desítky hodin
(world hints ~55 h, enrich_chapters 4–5 nocí, enrich_chunks velkých děl). Proto:

| Čas | Profil | Proč |
|---|---|---|
| **07–13** | comfy | interaktivní obrázky, video, audio, lab; **tributy 11:30** |
| **13–19** | director (denní směna) | stejné dávky jako v noci, resumují se |
| **19–01** | llm | večerní LLM práce (Právník, ToyShaders, FineTune VL), heartbeat **19:05–00:55**, tier 0 obrázky |
| **01–07** | director | noční dávky |

- Director 12 h denně místo 6 → fronta directora ubývá zhruba dvakrát rychleji.
- Přepnutí 4× denně po 3–10 min (director ~3 min + smoke test) ≈ 30 min ztráty, to se vyplatí.
- Chat Knihovníka a Právníka v okně director odpovídá directorem (pomaleji, ~1–2 min; 4 sloty z 16 jsou jeho).
- Kirian: video jen do 12:20 (40 min před koncem okna); comfy okno je známé.
- Co tím ztratíme: ComfyUI odpoledne a večer (jen flux-schnell tier 0 v llm okně). Večerní lab nebo tier 2
  dávky by šly přes frontu (fáze 2) do comfy okna druhý den.

## 4. Fronta dávek (fáze 2)

Projekty dnes pouštějí dávky ručně přes `systemd-run`. Fáze 2 zavede jednoduchou frontu na SPARKu:

- `~/spark-queue/<profil>/<priorita>-<projekt>-<jméno>.sh` — dávka = skript, který jen volá HTTP (LiteLLM, ComfyUI,
  gen-queue) a je resumovatelný. Nic nestartuje ani nemaže kontejnery.
- Plánovač při startu okna pustí dávky svého profilu podle priority; při konci okna je pošle `SIGTERM` (resume příště).
- `gemma` se aktivuje, jen když v `~/spark-queue/gemma/` něco čeká; jinak běží `llm`.
- Prázdné okno se smí zkrátit: když v comfy okně 2 h nic nerenderovalo a ve frontě čeká dávka jiného profilu,
  plánovač přepne dřív (ComfyUI `/free`, pak další profil).
- Teplotní pojistka a CPU-render hlídač z benchmarku jdou do plánovače (pauza nad GPU 87 / zóna 98 °C,
  `/interrupt` jen vlastním jobům).

## 5. Synchronizace sessions

Rozvrh i frontu vlastní session **Director**. Ostatní sessions:
- nespouštějí ani nemažou kontejnery, posílají jen HTTP;
- dávku zařadí souborem do `~/spark-queue/…` (fáze 2) nebo požádají Director;
- interaktivní práci dělají v okně svého profilu (§1), časy berou odsud.

Dotázáno 2026-10-01: ToyShaders, Kindlify, AiSwarmBattle, Kirian, Stickers, SwypeKids, FineTune odpověděli;
Aukrofy ne.

## 6. Cold projekty — co je odblokuje

| Projekt | Blokuje | Až se odblokuje |
|---|---|---|
| AiSwarmBattle | review evaluatoru (větev `phase2/evaluator`, služba v `docker-compose.swarm.yaml` necommitnutá); fáze 1 a 3 nezačaté | vrátit Nano + embed + swarm-litellm do profilu llm (WorldLibraryProject PR #9 — zavřít, nahrazeno tímto plánem); fáze 3 (celý Nemotron stack) potřebuje vlastní profil, do llm se nevejde |

## 7. Fáze implementace

| Fáze | Co | Stav |
|---|---|---|
| 1 | profily comfy / llm / gemma / director, okna 07/17/01, `library-chapters` + `storyteller-night` v noci, translate ven, tributy 16:30, heartbeat 17:05–00:55 | **nasazeno 2026-10-01** (WorldLibraryProject 8857abc, PromoClown PR #2) |
| 1b | llm = qwen36 + flux-schnell (Nano/embed ven, cold); okna podle §3b; tributy 11:30, heartbeat 19:05–00:55; sonda toleruje 1 variantu useknutou na délce | **nasazeno 2026-10-01 14:16** (WorldLibraryProject PR #11, PromoClown PR #3) |
| 2 | fronta dávek + gemma na vyžádání + zkracování prázdných oken | |
| 3 | přehled (profil, fronta, paměť, teploty), notifikace jako dnes `notify.sh` | |

## 8. Rozhodnuto 2026-10-01

1. Okna podle §3b — ano.
2. Kindlify: zh.daodejing + zh.lunyu předřazeny (jednorázový `kindlify-zh` 15:13), pak `library-chapters`
   abecedně; Kindlify průběžně přidává díla s hotovými kapitolami (export řeší session Kindlify).
3. ComfyUI okno 07–13 je hlavně pro experimenty uživatele (Ol1nLLM appka, lab).
