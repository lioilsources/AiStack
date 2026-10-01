# PLAN-spark-scheduler.md — plné využití SPARKu pro víc projektů

Stav 2026-10-01. Navazuje na `PLAN-model-bench.md` (naměřené modely §6a) a nahrazuje pevný rozvrh tří oken
v `WorldLibraryProject/deploy/spark/rag-schedule.sh` (comfy 07–00, promo 00–01, rag 01–07).

**Proč:** dnešní rozvrh zná jen ComfyUI, PromoClowna a Knihovníka. Ostatní projekty (ToyShaders, Právník,
AiSwarmBattle, Kindlify, LLM fáze StoryTelleru) nemají kam běžet, takže se pouštěly ručně mimo rozvrh a dvakrát
to skončilo incidentem (29. 9. CPU render + přehřátí, 30. 9. qwen36 chyběl v promo okně). SPARK přitom přes den
často stojí: ComfyUI je nahoře, ale nic nerenderuje.

---

## 1. Kdo co potřebuje (priorita podle uživatele)

| # | Projekt | Co na SPARKu | Paměť | Druh zátěže | Zdroj |
|---|---|---|---|---|---|
| 1 | **StoryTeller** | a) LLM fáze RAG (cards, hints, verbalize) na directoru; b) obrázky v ComfyUI / flux-schnell (tier 2, lab); c) audio | a) 91 GiB; b) 52 + 17 GiB; c) 25 GiB | dlouhé dávky (svět: ~12k jednotek hints ≈ 70 h directora), obrázky po hodinách | tento repo |
| 2 | **ToyShaders** (MirrorBooth ShaderGen) | jen LLM přes LiteLLM: coder `qwen36` (umí i vidění), nebo Nano-30B + `vl` | 36,5 nebo 27 + 20 GiB | dávka ~1 h 1–2× týdně, občas interaktivně; první ostré běhy za pár dní | session ToyShaders |
| 3 | **Knihovník** | `library-enrich` (enrich_chunks, 12 workerů) na directoru; `library-chat` volá řetěz translate → director → fallback | 91 GiB v noci; chat potřebuje model i přes den | noční dávka + interaktivní chat | rag-schedule |
| 4 | **Právník** | `law-chat` (e5, 1,3 GiB, běží pořád); chat LLM; agent → **Gemma-4** (bench: 92/100 %, injection 100 %) | Gemma 36,5 GiB (pro 12k kontext util 0,40 ≈ 49 GiB) | interaktivní, agent pomalý (~4–5 min/návrh) | bench §6a |
| 5 | **Kindlify** | nic vlastního; potřebuje `enrich_chapters` (souhrny kapitol) na directoru ve stejném okně jako Knihovník | 0 navíc, sdílí director | dávka: 101 kapitol ≈ 10–20 min, priorita 1 = 12 062 kapitol ≈ několik nocí | session Kindlify |
| 6 | **AiSwarmBattle** | swarm-nano, swarm-coder (občas), swarm-embed, ChromaDB, swarm-litellm; nový swarm-evaluator (EGL, < 1 GiB) | Nano 27 + embed ~6 GiB | interaktivní vývoj, dávky až ve Fázi 3 | session AiSwarmBattle |
| 7 | **Aukrofy** | neodpověděl | ? | ? | doplnit |
| – | mimo frontu | PromoClown (qwen36 heartbeat, tributy přes ComfyUI ve 23:00), Kirian trailer (video-stack v ComfyUI), MusicStudio (audio přes den) | | | |

## 2. Profily místo pevných režimů

Profil = sada služeb, která se do paměti vejde **se zachováním pravidel z incidentů**: ComfyUI render nikdy vedle LLM ≥ 30 GiB,
kontejnery jen `docker stop` (nikdy `compose down`), před startem `mem-admit` + kontrola KV cache. Základ (OS, LiteLLM,
gateway, chroma, law-chat, library-chat, postiz, kontejnery bez GPU) ≈ 20 GiB.

| Profil | Nahoře | GiB | Slouží | Pozn. |
|---|---|---|---|---|
| **comfy** | ComfyUI (≤ 52) + audio (25) + flux-schnell NIM na vyžádání (17) | ~95–115 | StoryTeller obrázky a lab, Kirian video, PromoClown tributy, MusicStudio | jako dnes; žádný LLM ≥ 30 GiB |
| **llm** (nový) | qwen36 (36,5) + Nano-30B (util 0,22 = 27) + swarm-embed (~6) | ~90 | Právník chat, ToyShaders (Nano coder + qwen36 vidění), AiSwarmBattle (swarm-nano = tentýž Nano), Knihovník chat, PromoClown | nahrazuje promo; `vl` (Qwen2.5-VL) netřeba, vidění dělá qwen36 |
| **gemma** (nový, na vyžádání) | Gemma-4 (util 0,40 ≈ 49) + Nano (27) | ~96 | agent Právníka, ToyShaders | místo **llm**, když je ve frontě práce pro agenta |
| **director** | swarm-director 0,75 (91) sám | ~111 | Knihovník enrich_chunks + Kindlify enrich_chapters + StoryTeller RAG dávky | jako dnešní rag; sloty se dělí |

**translate** (Qwen3-32B, TRT-LLM) z rozvrhu vypadne: bench ho ve všem předčí qwen36 (6× rychlejší, 64k kontext,
nástroje, JSON schéma) a v profilu **llm** je qwen36 stejně nahoře. Řetěz Knihovníka v LiteLLM se změní na
`translate → openclaw-default (qwen36) → swarm-director → fallback`, takže chat odpovídá v každém okně.

## 3. Denní rozvrh (fáze 1)

| Čas | Profil | Proč tady |
|---|---|---|
| **07–17** | comfy | denní obrázková práce (tvoje interaktivní, StoryTeller art, lab, Kirian), MusicStudio |
| **16:30** | comfy | PromoClown tributy (přesun z 23:00, kdy ComfyUI ještě běží — tribut po 23:00 dnes čeká do druhého dne) |
| **17–01** | llm | večerní LLM práce: Právník, ToyShaders, AiSwarmBattle, Knihovník chat; PromoClown heartbeat (rozšířit `activeHours` z 00:08–00:55 na 17:05–00:55) |
| **01–07** | director | Knihovník enrich_chunks + Kindlify enrich_chapters + StoryTeller RAG (fronta) |
| na vyžádání | gemma místo llm | když je ve frontě agent Právníka (např. út a čt večer) |

Přepnutí trvá 3–10 min (qwen36 ~2 min, Nano ~5 min, director ~3 min, ComfyUI hned), proto slot minimálně 2 h.

**Dělení noci (director, 16 slotů):** chat 4, zbylých 12 podle priority: Knihovník (3) > Kindlify (5) > StoryTeller (1),
ale Kindlify má dávku s koncem. Návrh: enrich_chunks 6 workerů + enrich_chapters 4 + StoryTeller hints 2, dokud Kindlify
nedokončí prioritu 1 (několik nocí); pak workery zpět Knihovníkovi.

## 4. Fronta dávek (fáze 2)

Projekty dnes pouštějí dávky ručně přes `systemd-run`. Fáze 2 zavede jednoduchou frontu na SPARKu:

- `~/spark-queue/<profil>/<priorita>-<projekt>-<jméno>.sh` — dávka = skript, který jen volá HTTP (LiteLLM, ComfyUI,
  gen-queue) a je resumovatelný. Nic nestartuje ani nemaže kontejnery.
- Plánovač při startu okna pustí dávky svého profilu podle priority; při konci okna je pošle `SIGTERM` (resume příště).
- `gemma` se aktivuje, jen když v `~/spark-queue/gemma/` něco čeká; jinak běží `llm`.
- Prázdné okno se smí zkrátit: když v comfy okně 2 h nic nerenderovalo a ve frontě **llm** čeká dávka, plánovač
  přepne dřív (ComfyUI `/free`, pak llm). To je to hlavní, co zvedne využití — dnes SPARK přes den hodiny stojí.
- Teplotní pojistka a CPU-render hlídač z benchmarku jdou do plánovače (pauza nad GPU 87 / zóna 98 °C,
  `/interrupt` jen vlastním jobům).

## 5. Synchronizace sessions

Rozvrh i frontu vlastní tahle session (Director). Ostatní sessions:
- nespouštějí ani nemažou kontejnery, posílají jen HTTP;
- dávku zařadí souborem do `~/spark-queue/…` (fáze 2) nebo požádají Director;
- interaktivní práci dělají v okně svého profilu (ToyShaders a AiSwarmBattle: llm 17–01; Kindlify: director 01–07).

Potvrzeno 2026-10-01: ToyShaders, Kindlify, AiSwarmBattle (žádné vlastní kontejnery, nespěchají). Aukrofy neodpověděl.

## 6. Fáze implementace

| Fáze | Co | Riziko |
|---|---|---|
| 1 | `rag-schedule.sh`: profily comfy / llm / director (promo = alias llm), okna 07/17/01, `enrich_chapters` do director okna s dělením workerů, translate ven, LiteLLM řetěz Knihovníka přes qwen36; `promo-tribute.timer` 16:30; Nano jako `bench/serve.sh nano` (util 0,22) nebo oprava compose 0,15 → 0,22 | střední: mění produkční rozvrh, PromoClown a Knihovník |
| 2 | fronta dávek + profil gemma na vyžádání + zkracování prázdných oken | nízké, přidává se vedle |
| 3 | přehled (stránka se stavem: profil, fronta, paměť, teploty), notifikace jako dnes `notify.sh` | nízké |

## 7. Rozhodnutí pro uživatele

1. Den rozdělit **comfy 07–17 / llm 17–01**? (Večerní obrázková práce by pak musela do fronty nebo počkat na ráno.)
2. **translate vyřadit** a Knihovníkův chat posílat přes qwen36?
3. **Gemma** pro agenta Právníka na vyžádání (fronta), nebo pevné večery?
4. PromoClown: tributy v 16:30 a heartbeat 17:05–00:55?
5. Noc: dělení workerů Knihovník / Kindlify / StoryTeller podle §3, dokud Kindlify nedožene prioritu 1?
