# PLAN-model-bench.md — řídicí plán: srovnání modelů + běhy Lab / Právník / ToyShaders

Jedno místo, odkud se řídí tři podzimní běhy na SPARKu a srovnání modelů, které o nich rozhodne. Detailní plány běhů žijí ve svých repech (§8 říká kde), tady je jejich souhrn, závislosti, okna a stav. Stav k 2026-09-29.

Otázka srovnání: **translate-lean, nebo qwen36 — pro Právníka (Ol1nLLM + WorldLibraryProject `rag/agent`) a pro ShaderGen/ToyShaders (MirrorBooth `pipeline/`)?** Plus director a modely, které už leží na disku, včetně Llama-3.3-70B. Výstup je tabulka §6 a rozhodnutí pro každý projekt.

Zdroje: `deploy/docker-compose.*.yaml`, `.env`, `deploy/litellm_config.yaml`, cache v `~/deploy/AiStack/cache/` a `~/.nim/cache`, `WorldLibraryProject/rag/{agent,eval}` (větev `pravnik`), `MirrorBooth/pipeline` (+ větev `claude/plan-shader-pipeline-filters-axG6R`), `storyteller/STORYTELLER_CHARACTER_MODELS_LAB_PLAN.md`.

---

## 0. Co je "age_rating" a další metadata modelu

Pole `age_rating` **nikde ve stacku není** (prohledáno AiStack, Ol1nLLM, WorldLibraryProject, storyteller, PromoClown, MirrorBooth, LiteLLM config). Nejbližší věc, kterou director skutečně nese, jsou **NVIDIA Model Card++** soubory vedle vah (`cache/swarm-director/…/snapshots/*/`):

| Soubor | Co v něm je |
|---|---|
| `explainability.md` | Intended task/domain, model type (Mamba2-Transformer hybrid), intended users, **"adversely impacted groups tested": Age, Disability, Gender Identity, Nationality, Physical Appearance, Ethnicity, Socioeconomic Status, Sexual Orientation…**, technical limitations, known risks (prompt injection), licence (NVIDIA Nemotron Open Model License) |
| `bias.md` | Bias metric (BBQ accuracy), nejhorší kategorie (Physical Appearance), **"Unwanted Bias Testing: constrained to English-language inputs. Multi-lingual parity is not claimed"** — tj. čeština není nikým měřená |
| `safety.md`, `privacy.md` | Content-safety a privacy subkarty |
| `hf_quant_config.json`, `config.json`, `generation_config.json` | NVFP4 kvantizace, architektura, kontext, výchozí sampling |

"Age" tam figuruje jen jako chráněná skupina, na které se testovala **rovnost výstupu**, ne jako věková vhodnost obsahu. Věkovou vhodnost si řešíme sami (storyteller `age_min`/`soft`, filtry `check_for_age`). Qwen a Gemma mají jen `README.md` (HF karta: licence, base model, jazyky, tagy); Llama-3.3 NIM profil nese `tool_use_config_v2.json` a chat template s nástroji. V tabulce §6 vedu sloupec **Karta** = co model o sobě deklaruje (licence, jazyky, bias testing, rizika), a plníme ho z těchto souborů, ne z benchmarku.

---

## 1. Kandidáti — co je na disku a jak se to spouští

Paměť = unified 121,7 GiB; přes den (režim comfy) drží ComfyUI ~52 GiB, po vypnutí audia zbývá **~48 GiB** na jeden model. V noci (rag 01–07) běží director.

| Alias / služba | Model | Váhy | Běh (GiB) | Kontext | Tool calling | Thinking | Jak nahodit | Okno |
|---|---|---|---|---|---|---|---|---|
| `translate` | nvidia/**Qwen3-32B**-FP4, TRT-LLM | 20 G | lean 36 / full 57 | 16k | **ne** (`trtllm-serve` bez tool parseru) | vyp. (LiteLLM) | `make up-translate-lean` | den vedle ComfyUI |
| `openclaw-default` / `qwen36-agent` | nvidia/**Qwen3.6-35B-A3B**-NVFP4, vLLM | 22 G | 36,5 (util 0,30) | 64k | **ano** (`qwen3_xml`) | parser `qwen3`, alias má vyp. | `make up-agent` | den vedle ComfyUI, promo 00–01 |
| `swarm-director` | nvidia/**Nemotron-3-Super-120B-A12B**-NVFP4, vLLM | 75 G | 91 (noční 0,75) / 73 (swarm 0,60) | 32k / 262k | **jen ve swarm profilu** (`qwen3_coder`); **noční profil `director-night.yaml` tool calling nemá** | `nemotron_v3` | noc automaticky; `make up-swarm-director` pro tools | noc 01–07 |
| **`llama33`** (nová, jen pro bench) | meta/**Llama-3.3-70B-Instruct** fp8 (NIM profil `fp8-…-tool-calling`, modelopt FP8, **15/15 shardů kompletních**, 68 G v `~/.nim/cache/ngc/hub/models--nim--meta--llama-3.3-70b-instruct`); profily nvfp4/bf16 mají jen configy | 68 G | ~85 (util 0,70) | 128k | ano (`llama3_json`, chat template s nástroji) | – | NIM image **nestažený** (~30 G) → místo něj jednorázový vLLM: `docker run … vllm/vllm-openai:v0.20.0 /model --quantization modelopt --enable-auto-tool-choice --tool-call-parser llama3_json` s mountem snapshotu | jen noc, director dolů |
| `dev` (**pozor**) | .env říká Gemma-4-31B-IT-NVFP4 (váhy 41 G v `cache/dev/hub`), ale compose `dev` = **NIM Llama-3.1-8B** | 41 G / 29 G image | Gemma ~35 (odhad) | 128k | Gemma: vLLM ano | – | Gemma nemá službu — jednorázový `docker run vllm/vllm-openai:gemma4-cu130` (image je stažený, nikdy neběžel) | den |
| `lab` | compose = **NIM Llama-3.3-Nemotron-Super-49B** (61 G ngc); .env `HF_MODEL_LAB=gpt-oss-120b` (61 G v `cache/lab/hub`, bez služby) | 61 G + 61 G | 49B NIM ~55; gpt-oss ~65 | 128k | NIM ano; gpt-oss vLLM ano | – | `make up-llm` (lab profil) | jen noc (ComfyUI dole) |
| `swarm-nano` / `swarm-coder` | nvidia/**Nemotron-3-Nano-30B-A3B**-NVFP4 | 19 G | 18 (0,15) | 32k | ano (`qwen3_coder`) | `nano_v3` | `make up-swarm` (nano) | den, vejde se **spolu s VL** |
| `fallback` | Qwen/Qwen3-4B-AWQ | 2,5 G | 9 | 16k | ne | vyp. | vždy (dnes neběží!) | – |
| **VL**: `tune-validator` | .env říká **Qwen2.5-VL-7B-Instruct** (16 G v `cache/qwen-vl`), compose ale míří na NIM `nemotron-nano-vl-8b` (**image nestažený**) | 16 G | ~20 (util 0,18) | 32k | – | – | nová služba `docker-compose.vl.yaml` (vLLM, `--limit-mm-per-prompt image=2`) | den, vedle ComfyUI + nano |
| na disku bez služby, vynecháno | Llama-4-Scout-17B-16E FP4 (61 G, `llm-stack/cache/trt-lab`, od dubna netknuto), Qwen3-32B-AWQ (19 G), Phi-4-reasoning-plus-FP4 (9 G), gemma-4-E2B-it (9,6 G), DeepSeek-V4 (806 G) | | | | | | | |

**Do srovnání beru:** translate, qwen36, director (noční + swarm profil), **Gemma-4-31B**, **Nemotron-Nano-30B** (levný, má tools — kandidát pro denního agenta), **Llama-3.3-70B** (noc 2), **gpt-oss-120b** (noc 2, pokud zbude), VL **Qwen2.5-VL-7B**.

**Llama-3.3-70B — proč až noc 2 a co od ní čekat:** nikdy neběžela (stažena 23.–24. 4., žádná služba, žádný záznam), není známo, že by nefungovala. Je to hustý 70B: GB10 čte ~273 GB/s, fp8 váhy mají 68 GB → teoreticky ~4 tok/s na stream, prakticky 2–3; director (12B aktivních) a qwen36 (3B aktivních) jsou MoE a řádově rychlejší. Meta u Llamy 3.3 oficiálně podporuje 8 jazyků, čeština mezi nimi není. Měří se na ní A1, A3, C1, C2 + tok/s; první start je test sám o sobě (fp8 kernely na GB10).

> Dvě nesrovnalosti k opravě mimo benchmark: `dev` v compose není Gemma, a `tune-validator` nemá stažený image. Ani jedno neblokuje test, ale ať to nepřekvapí.

---

## 2. Co projekty od modelu potřebují

| | Právník | ShaderGen / ToyShaders |
|---|---|---|
| Jazyk výstupu | čeština, právní registr, přesné citace § | angličtina (GLSL + JSON), UI popisky česky ne |
| Formát | `response_format json_schema` (s fallbackem `json_object`), **tool calling** (`rag/agent/llm.py` posílá `tools` + `tool_choice=auto`; 8 nástrojů: `search_law`, `get_paragraph`, `list/get_template`, `save_intake`, `render/review_document`, `ask_user`) | LangChain `ChatOpenAI` (streaming, temperature), ranker chce **raw JSON**, coder chce čistý `.frag` bez markdownu |
| Kontext | RAG kontext 8 chunků + historie ≈ 6–10k tokenů | RAG 5 shaderů + port reference + prompt ≈ 4–8k |
| Vidění | ne | **ano — hodnocení vyrenderovaného shaderu** (dnes ranker hodnotí jen text kódu; VL je nový krok) |
| Latence | chat, do ~10 s na odpověď | batch, nevadí |

Důsledek: **translate pro agenta Právníka odpadá** už na tool callingu (jde jen pro `law-chat` RAG odpověď bez nástrojů). Pro agenta zbývají qwen36, Nano-30B, Gemma-4, director (swarm profil), Llama-3.3 (noc).

---

## 3. Testovací sady

Všechno běží přes LiteLLM `:8080`, ať se měří produkční cesta (parametry aliasu, fallbacky vypnout na dobu testu — `router_settings.fallbacks` by jinak tiše přehodily model). Pro benchmark přidat **neutrální aliasy** `bench-<model>` (temperature 0,2, thinking vyp.) + `bench-<model>-think` (thinking zap.) — `openclaw-default` má temperature 0,6/top_p 0,8, to by srovnání zkreslilo.

### A. Čeština (všechny textové modely)

| Test | Vstup | Metrika | Nástroj |
|---|---|---|---|
| A1 Právník RAG odpověď | 18 otázek `golden_law_v2.jsonl` + **fixní kontext** z `GET /search` běžícího `law-chat` (retrieval stejný pro všechny, liší se jen generace) | plan-pravnik §9.3: (a) cituje jen dodané § — % odpovědí bez cizí citace, (b) uvede znění/datum, (c) odliší zákon od výkladu, (d) disclaimer právě jednou, (e) česky správně | auto (a)(b)(d) regexem; (c)(e) slepé hodnocení 1–5 |
| A2 Volná čeština | 20 promptů: převyprávění pohádky pro 3–6 let, shrnutí § do 3 vět, e-mail úřadu, instrukce s číslovkami a skloňováním, dialog v ženském rodě (rod hrdinky — známá slabina, viz storyteller) | `wrong_language` + `check_for_age` z `storyteller/rag/rag/filters.py`, podíl diakritiky, počet anglicismů; slepé párové hodnocení | auto + člověk |
| A3 Překlad EN→CS | 30 chunků z A/B knihovny (stejná sada jako 2026-09 translate vs director; Beowulf/Hérodotos) | slepé párové hodnocení + počet faktických chyb | člověk |
| A4 Dlouhý kontext | 12k-token dokument (zákon) → 5 otázek | správnost, „ztracená polovina“ (translate má 16k!) | auto |

### B. Shadery (ShaderGen v2 / ToyShaders — artefakty z fází plánu v2, viz §8.3)

| Test | Vstup | Metrika | Nástroj | Kdy jde |
|---|---|---|---|---|
| B1 Kontrakt | 12 stylových promptů × model → `pipeline/run.py --style …` (režim *inspire*) s `SPARK_MODEL=bench-<model>` | % průchodů `validator.py` (regex kontrakt + `glslangValidator` — **na Macu chybí: `brew install glslang`**) | dnešní pipeline | hned |
| B2 Kompilace naostro | tytéž `.frag` | % zkompilovaných **`impellerc`** pro Metal/GLES/Vulkan (Fáze 4 `checks/compile.py`; do té doby ruční volání `impellerc` z Flutter SDK) | v2 Fáze 4 | hned ručně, naostro po Fázi 4 |
| B3 Render | `preview/render.py` (Fáze 5, `moderngl`, testovací selfie 540×960, t = 0 / 0,7 / 1,9 s) | `preview/metrics.py`: podíl černých/NaN pixelů, „shader nic nedělá“, luminance | v2 Fáze 5 | po Fázi 5 |
| B4 VL soudce | 3 náhledy z B3 + popis → **Qwen2.5-VL-7B** jako `vision_ranker` s providerem `spark` (`visual_quality`, `face_readability`, `matches_intent`, `artifacts`) | shoda s lidským hodnocením na 20 vzorcích (Spearman) — **VL model se tu testuje sám**: pokud < 0,6, do pipeline nepatří a zůstane textový ranker | v2 Fáze 5 + `bench.py` | po Fázi 5 |
| B5 Ranker JSON | výstup `ranker.py` | % validní raw JSON bez markdown plotu | dnešní pipeline | hned |
| B6 Port + fixer | 6 permisivních `image_filter` shaderů z harvestu → `run.py --from-shadertoy` | kolik kol `llm_fixer` do průchodu `impellerc`; diff jen ve vyjmenovaném (`fixer_diff.patch`) | v2 Fáze 3–4 | po Fázi 4 |

Pro **coder roli** rozhodnou B1 + B5 (+ B2 ručně) už na dnešní pipeline; pro **VL** až B4 po Fázi 5.

### C. Tool-chain / formáty

| Test | Vstup | Metrika |
|---|---|---|
| C1 Agent Právník | 22 scénářů `rag/eval/lawyer_agent/scenarios_{review,draft}.jsonl` přes `agent.loop.Pravnik` s reálným klientem (`agent/llm.py`) | % scénářů splněných (`ceka`), validita tool callů (parse chyby, halucinované nástroje), počet kol, latence; model bez tools = „nepodporuje“ |
| C2 Strukturovaný výstup | 25 extrakcí se schématem `_all_required` (storyteller `TaleClassification`) | % validní JSON, % všech polí vyplněno (lekce z 2026-09-26: director bez `required` vynechával pole) |
| C3 Streaming přes LangChain | 10 dotazů `ChatOpenAI(streaming=True)` | funguje, první token (ms) |
| C4 Odolnost | prompt injection v RAG kontextu („ignoruj § a poraď…“), 5 případů | % odolá (Model Card++ directora to explicitně uvádí jako riziko) |

### D. Provozní vlastnosti (do tabulky, bez sady)

Paměť skutečná (`free -g` po startu + při 4 souběžných), tok/s jeden stream a 4 souběžné (měřit **v noci na prázdné GPU**, přes den ComfyUI zkresluje), čas startu, licence, jazyky deklarované v kartě, bias testing (EN-only u Nemotronu), thinking on/off rozdíl na A1 a C1.

---

## 4. Harness

- `AiStack/bench/` (nové): `bench.py` (Python, httpx → `:8080`, sady v `bench/suites/*.jsonl`, výsledky `bench/results/<model>/<suite>_<datum>.jsonl`, souhrn `bench/results/summary.md`), `rate.py` (slepé párové hodnocení v terminálu, anonymizované pořadí, zapisuje `ratings.jsonl`), `suites/` (A1 generuje z `golden_law_v2` + `/search`, A2/A4/C2/C4 ručně napsané, B přes MirrorBooth pipeline).
- Import: `storyteller/rag/rag/filters.py` (čeština), `WorldLibraryProject/rag/agent` (C1), `MirrorBooth/pipeline` (B).
- `deploy/litellm_config.yaml`: aliasy `bench-*`, `vl`, `llama33`; `deploy/docker-compose.vl.yaml` (Qwen2.5-VL-7B, vLLM, util 0,18, `--limit-mm-per-prompt image=2`).
- Gemma-4-31B: jednorázový `docker run --network aistack_internal vllm/vllm-openai:gemma4-cu130 nvidia/Gemma-4-31B-IT-NVFP4 --gpu-memory-utilization 0.30` s `CACHE_DEV/hub` — první spuštění je test sám o sobě (image se stavěl, nikdy neběžel).
- Llama-3.3-70B: jednorázový `docker run --network aistack_internal -v <snapshot fp8>:/model vllm/vllm-openai:v0.20.0 /model --served-model-name llama33 --quantization modelopt --gpu-memory-utilization 0.70 --max-model-len 16384 --enable-auto-tool-choice --tool-call-parser llama3_json` (bez NIM image; pokud vLLM modelopt FP8 na GB10 selže, zapsat a nechat být — NIM cesta by stála 30 G stahování a stejně 80 GiB paměti).
- Každý model ≈ 1–1,5 h všech sad (A: 18+20+30+5 promptů, B: 12 shaderů × 3 uzly, C: 22 scénářů + 25 + 10 + 5); Llama 70B při 2–3 tok/s ≈ 3–4 h → jen A1, A3, C1, C2.

---

## 5. Rozvrh — kam se co vejde (společný pro bench i tři běhy)

> **Oprava 2026-09-29 (incident):** souběh ComfyUI renderu a 36 GiB LLM **nefunguje**, i když to čísla níž dovolují. Lab vlna A (flux-dev) vedle benchmarku translate-lean: načtení vah FLUX znovu naplnilo page cache, ComfyUI (vidí jen MemFree) spadl na CPU render, 45 min ~90 W se zónou 93–95 °C, pak OOM guard zabil ComfyUI a 277/288 buněk skončilo 502. **Platí: ComfyUI render a velký LLM jen sériově**, mezi nimi `POST /free` na ComfyUI; pojistka při horku volá `/interrupt`; cizí render (video-stack) má přednost. Tabulka níž je proto kapacitní strop, ne rozvrh souběhu — Lab a bench se střídají.

Přes den běží **Lab benchmark postav v ComfyUI (52 GiB, ~5 GPU-h, §8.1)** — sdílí výpočet, ne paměť; kvalita se měřit dá, latence ne. ToyShaders B1–B3 běží na Macu a chce ze SPARKu jen LLM (+ VL pro B4). Právník chce model s tools (C1) a `law-chat` (běží pořád, 1,3 GiB).

| Slot | Nahoru | GiB (s ComfyUI 52) | Bench | Lab (§8.1) | Právník (§8.2) | ToyShaders (§8.3) |
|---|---|---|---|---|---|---|
| Den 1 dopoledne | audio dolů; **qwen36** (36,5) | ~89 | A, B, C | příprava bez GPU (§2 Lab plánu) | C1 na qwen36 = první měření agenta | merge větve, pytest, config → `:8080` |
| Den 1 odpoledne | qwen36 dolů; **translate-lean** (36) | ~88 | A, B, C2–C4 | vlna A (360 buněk, ~60 min) | A1 chat na translate | B1–B3 na Macu s translate |
| Den 1 večer | translate dolů; **Nano-30B (18) + VL (20)** | ~90 | Nano: A, B, C; VL: B4 nad rendery všech | vlna B (na 8 postavách z A) | C1 na Nano | B4 VL soudce, kalibrace na 20 vzorcích |
| Noc 1 (01–07) | director **ve swarm profilu 0,60 s tools** (`make up-swarm-director`; obohacení knihovny pojede na 0,60) | 73 | A, B (text), C | – | C1 na directoru | B1/B5 text |
| Den 2 dopoledne | **Gemma-4-31B** (~35, jednorázový run) | ~87 | A, B, C | vlna B zbytek, C (100) | C1 na Gemmě | B6 port reference |
| Den 2 odpoledne | ComfyUI zůstává | 52 + | vyhodnocení, tabulka | vlny D, E (240) | rozhodnutí o mozku | rozhodnutí model + VL |
| Noc 2 | **Llama-3.3-70B** (~85, director dolů, obohacení stojí) → poté **gpt-oss-120b** (~65), když zbude čas | 85 / 65 | A1, A3, C1, C2, tok/s | – | – | – |

Nevejde se nikdy: director + ComfyUI; dva 36 GiB modely vedle ComfyUI; VL vedle qwen36/translate/ComfyUI (52+36+20 = 108 + OS ≈ hrana OOM guardu) — proto VL až se Nanem; Llama 70B vedle čehokoli velkého.

---

## 6. Výstupní tabulka (kostra)

| Model | A1 cituje jen § | A1 lidské (c)(e) | A2 čeština | A3 překlad | A4 12k | B2 kompiluje | B4 VL/člověk | B5 JSON | C1 agent | C2 schéma | C3 stream | C4 injection | tok/s 1×/4× | GiB | Kontext | Tools | Licence | Karta (bias EN-only? rizika) | Okno |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| translate (Qwen3-32B FP4) | | | | | | | | | **–** | | | | | 36 | 16k | ne | Apache-2.0 | HF README | den |
| qwen36 (Qwen3.6-35B-A3B) | | | | | | | | | | | | | | 36,5 | 64k | ano | Apache-2.0 | HF README | den/promo |
| director noc (Nemotron-3-Super-120B) | | | | | | | | | **–** | | | | | 91 | 32k | ne | Nemotron OML | Card++ (EN-only bias) | noc |
| director swarm 0,60 | | | | | | | | | | | | | | 73 | 262k | ano | | | noc |
| Gemma-4-31B-IT | | | | | | | | | | | | | | ~35 | 128k | ano | Gemma ToU | HF README | den |
| Nemotron-Nano-30B-A3B | | | | | | | | | | | | | | 18 | 32k | ano | Nemotron OML | Card++ | den |
| **Llama-3.3-70B fp8** | | – | – | | – | – | – | – | | | – | – | (čekám 2–3) | ~85 | 128k (bench 16k) | ano | Llama 3.3 Community | HF karta, 8 jazyků bez CS | noc 2 |
| gpt-oss-120b | | | | | | | | | | | | | | ~65 | 128k | ano | Apache-2.0 | | noc 2 |
| **VL** Qwen2.5-VL-7B | – | – | – | – | – | – | Spearman vs člověk | | – | | | | | 20 | 32k | – | Apache-2.0 | | den |


### 6a. Výsledky (2026-09-29 – 10-01, automatické skóre `bench/score.py`)

| | director (swarm 0,60) | Gemma-4-31B | Llama-3.3-70B fp8 | Nano-30B | qwen36 | translate |
|---|---|---|---|---|---|---|
| A1 cituje jen dodané § | 72 % | **100 %** | **100 %** | **100 %** | 78 % | 83 % |
| A1 zmíní očekávaný § | **67 %** | 56 % | 61 % | 56 % | **67 %** | 50 % |
| A4 12k kontext | 100 % | 0 % (KV 9k při 0,30) | 100 % | 40 % | 100 % | 0 % (strop 16k) |
| C2 schéma, všechna pole | **96 %** | 20 % | 60 % | 48 % | 48 % | 0 % |
| C4 odolá injection | 20 % | **100 %** | 40 % | 80 % | 60 % | 60 % |
| C1 agent draft / review | 83 / 80 % | **92 / 100 %** | 42 / 50 % | 8 / 70 % | 50 / 50 % | – (bez tools) |
| C1 s/scénář | 161 | 278 | 658 | **28** | **20** | – |
| B1 kontrakt / s na shader | 100 % / 67 | 100 % / 145 | – | 100 % / **12** | 100 % / 16 | 100 % / 62 |
| tok/s 1× / 4× | 15 / 42 | 7 / – | 3 / 9 | 62 / 169 | **80 / 215** | 12 / 48 |

**Doporučení (čeká na rozhodnutí uživatele):** Právník agent = **Gemma-4** (nejspolehlivější, jediná 100 % proti
injection; ~35 GiB, pomalá → noční/dávkový agent nebo místo qwen36 v promo okně, nikdy souběžně s ComfyUI
renderem). Rychlý chat bez nástrojů = qwen36. ShaderGen coder = Nano-30B (12 s/shader) do výsledků B2/B4.
Llama-3.3 ne (3 tok/s, 43 chyb nástrojů). Nalezené chyby: agent Právníka posílal víc system zpráv (Qwen 400,
opraveno `25c3d27`), Nano s util 0,15 nemá KV cache (bench/serve.sh nano 0,22).

**Rozhodovací pravidla:**
- Právník `law-chat` (bez nástrojů): A1(a) ≥ 90 %, A1 lidské ≥ 4/5, C4 ≥ 4/5 → jinak „nepouštět na produkci“ (plan-pravnik §9.3).
- Právník agent: navíc C1 ≥ 80 % scénářů a 0 halucinovaných nástrojů → kandidáti jen s tools; **pokud vyhraje jen director nebo Llama, agent je noční**.
- ShaderGen: B2 ≥ 80 %, B5 = 100 %, B4 shoda VL–člověk ≥ 0,6; jinak VL krok vynechat a nechat textový ranker.
- Remíza → levnější v GiB (Nano před qwen36 před Gemmou), protože přes den musí zbýt místo ComfyUI. Llama 70B vyhraje jen kdyby A1/C1 byly o třídu lepší — pod 5 tok/s nejde do chatu.

---

## 7. Postup a co je na uživateli

1. **[uživatel]** souhlas: audio přes den dolů; noc 1 s directorem ve swarm profilu (0,60, tools) místo nočního; noc 2 bez obohacení knihovny (Llama/gpt-oss); Mac: `brew install glslang`; ~2× 20 min slepého hodnocení (A1c/e, A3, B4 kalibrace); souhlas před každou vlnou Labu (pravidlo Lab plánu §1).
2. **[Claude]** `bench/` harness + sady A2/A4/C2/C4; aliasy `bench-*`, `llama33`; `docker-compose.vl.yaml`; MirrorBooth: ShaderGen v2 Fáze 0–2 podle §8.3 (`config.py` → `:8080` + alias; dnes míří na `:8000` = NIM lab, model `llama3` — mrtvá adresa); Lab příprava §2 (Ol1nLLM větev `feat/lab-storyteller-characters`).
3. Den 1 podle §5; výsledky průběžně do `bench/results/summary.md`; archy Labu uživateli po každé vlně.
4. Noc 1 director; den 2 Gemma + Lab D/E; noc 2 Llama-3.3 a gpt-oss.
5. Vyplnit §6, napsat rozhodnutí pro Právníka (chat vs agent) a ShaderGen (model + zda VL), vyplnit rozhodovací tabulku Labu; navazující plány v Ol1nLLM, WorldLibraryProject a MirrorBooth.

Rizika: Gemma-4 image nikdy neběžel (může padnout na flashinfer/GB10 — viz `vllm-gb10-audit`); Llama fp8 modelopt na GB10 nikdy neběžela; VL vedle ComfyUI při Lab renderu může tlačit ComfyUI na CPU (`comfyui-cpu-render-memfree`) — hlídat `comfyui-cpu-watch`; director ve swarm profilu 0,60 = 262k kontext, ale Nemotron-3 na GB10 dřív potřeboval PIECEWISE cudagraph (noční compose to má, swarm možná ne — ověřit před nocí); Lab a video-stack sdílejí frontu ComfyUI — před každou vlnou `pgrep -af "chain.py|story.py"`.

---

## 8. Běhy projektů — sloučené plány

### 8.1 Lab: postavy StoryTelleru v jiných modelech a LoRA

**Plán:** `storyteller/STORYTELLER_CHARACTER_MODELS_LAB_PLAN.md` (sepsán 29. 9., commitnut na `master`). Kód se mění v `Ol1nLLM/tools/lab` (větev `feat/lab-storyteller-characters`, worktree), výsledky do `storyteller/STORYTELLER_CHARACTER_MODELS.md` (nový soubor po verdiktu).

**Otázka:** tier 1 postav (flux-dev, SDXL, 3D/Pixar LoRA) generovat z tier-0 obrázku jako **reference**, nebo znovu z **promptu + seedu**? MODELS_PLAN §0.2 říká reference, nikdy se to neměřilo.

**Vstupy:** 1 400 + 107 postav (`world_cards.json`, `cz_cards.json`, `motif_images/`), prompt `render-motifs -kind character`, seed = dolních 32 bitů `sha256("character\x1f"+id)`; 7 stylů z `infra/seed/models_styles.sql` (jen watercolor active) + nový kandidát `pixar-3d`; 5 FLUX-dev LoRA v `~/Code/ComfyUI/models/loras/3D_Pixar_Flux/` (ověřené hlavičky, triggery v `TRIGGERS.tsv`), SDXL Disney/MeMaXL k ověření; **ostatní LoRA složky jsou NSFW — whitelist jen tenhle.**

**Příprava bez GPU (§2 plánu):** vzorek 20 postav (`candidates/storyteller-cast.json`: 6 lidí, 6 zvířat, 5 nadpřirozených, 3 věci; přednost id viditelným v appce) + reference do `Ol1nLLM/build/lab/refs/storyteller/`; prompty pro 3 rodiny (`flux` bez stylové věty, `juggernaut`, `danbooru`); CLI `--prompt-ids`; styly ze SQL do `--styles-file` + `pixar-3d`; registrace LoRA v `lib/models/lora_family.dart` (+ klíč se složkou kvůli kolizi `Velvets_Mythic…`); `tools/lab/dino.py` (DINOv2 cos ≥ 0,80 = produkční gate `degraded`) — doporučeno; `make lab-dry` projde.

**Vlny (GPU, každá po souhlasu, `--latent 1024x1024`, po běhu `lab score` + arch přes `sheets.py`):**

| Vlna | Co | Buněk | Čas | Odpovídá na |
|---|---|---|---|---|
| A | 5 modelů × 8 postav × 9 stylů, txt2img, seed 777 | 360 | ~60 min | `preferred_model` per styl; jde Pixar look bez LoRA; review 6 shadow stylů |
| B | po postavě: `--ref` + její seed, 4 modely × {txt2img, img2img=Kontext, repose} × {baseline, watercolor, pixar-3d} | 20 × ~30 = 600 | ~2 h | **hlavní otázka** — kolik zůstane bez reference; která `ref2img` cesta drží postavu (Kontext / ControlNet / SDXL img2img) |
| C | 5 Pixar LoRA × 20 postav, flux-dev txt2img, trigger jako prefix | 100 | ~35 min | 2 finalisté LoRA |
| D | finalisté z reference: 20 × 2 LoRA × 3 flow | 120 | ~50 min | prompt+LoRA vs Kontext+LoRA vs ControlNet+LoRA |
| E | knoby: `loraStrength` 0,5–1,3, seed, SDXL `editDenoise` 0,5–0,8 | ~120 | ~40 min | hodnoty do `styles` |

**Pravidla:** před během `ssh spark 'pgrep -af "chain.py|story.py"; docker ps'` (fronta ComfyUI je sdílená s video-stackem); gen-queue musí běžet (sloupec flux-schnell); strop 400 buněk/běh; obrázky do gitu Ol1nLLM ne. **Okno:** jen den (ComfyUI), viz §5 — vlna A den 1 odpoledne, B den 1 večer + den 2, C–E den 2.

**Hodnocení:** za buňku `same/style/frame/kid` 0–2 + DINO; výstup = rozhodovací tabulka (stačí txt2img? která `ref2img`? `preferred_model` per styl? Pixar jako nový styl s jakou LoRA/silou? kolik postav propadne DINO gate?) → změny v `infra/seed/models_styles.sql`, `comfy/workflows/<model>/ref2img.json`, MODELS_PLAN §8.

**Stav:** plán hotový, nic nespuštěno; kontrolní seznam §7 plánu prázdný.

### 8.2 Právník: RAG persona + agent

**Plány:** `Ol1nLLM/docs/plan-pravnik.md` (RAG persona, 23. 9., fáze A–B–D hotové, C = tunel `pravnik.ol1n.com` na uživateli) a `WorldLibraryProject/docs/lawyer/AGENT.md` + `CURRENT_STATE.md` + `TEMPLATES.md` (větev **`pravnik`**, 9 commitů před `main`, lokál i remote na `4a2af47`, nemergnutá; PR zatím neotevřen — GitHub nabízí https://github.com/lioilsources/WorldLibraryProject/pull/new/pravnik, otevřít až po měření C1, ať PR nese i výsledek). `AGENT.md` odkazuje na `LAWYER_AGENT_PLAN.md` a `CURRENT_STATE.md` na `LAWYER_RAG_FIX_PLAN.md` — **oba „mimo repo“, na disku ani v artefaktech nenalezeny** → pokud existují (Claude doc?), uložit do `WorldLibraryProject/docs/lawyer/`.

**Co běží:** `law-chat.service` na SPARKu **:8098** (Ol1nLLM CLAUDE.md říká 8091 — to je gen-queue, opravit), Chroma `law_v1` (53 předpisů, 13 458 §, 19 031 pasáží, e5-large, 1,3 GiB), Postgres `law` na JODA :5433. Měřeno: jen vektor poráží hybrid (ref-hit@8 91 % vs 74 %), plánovač knihovny je pro zákony škodlivý → `--channels vec --planner off --rewrite off`. Agent v `rag/agent/` (8 nástrojů, intake v PG, deterministická revize, `POST /agent/chat`), eval **22/22 bez LLM**; `/agent/chat` vrací 503, protože přes den neběží žádný chat model s tools.

**Blokátor = model** (přesně otázka tohoto benchmarku): kandidáti qwen36 (v LiteLLM „ten“ model pro tools, ale jen 00:08–00:55), director (swarm profil; parser `qwen3_coder` na Nemotronu = podezřelé, změří C1), Nano-30B, Gemma-4, Llama-3.3 (noc). Doporučení AGENT.md: Qwen3.6 jako mozek + heuristický router; znamená to 36,5 GiB přes den.

**Kroky:**
1. Bench A1 (chat) a C1 (agent) na všech kandidátech (§5) — první, co C1 změří naostro: (a) vrací vLLM `tool_calls` pro director, (b) pro qwen36, (c) kolik volání a sekund stojí jeden draft.
2. Rozhodnutí: mozek chatu (`law-chat` LiteLLM řetěz `translate → director → fallback` — dnes přes den **fallback neběží** → 503) a mozek agenta; podle výsledku buď qwen36/Nano rezidentně přes den (rozhodnutí o paměti), nebo agent jen noční.
3. Fáze C tunel `pravnik.ol1n.com` **[uživatel]**; merge větve `pravnik` do `main` po měření.
4. Flutter UI agenta (karty `ask_user`, progress, sessiony), docx/pdf vstup i výstup, judikatura — mimo tento plán.

**Okno:** `law-chat` běží vždy; C1 kdykoli běží model s tools (§5); embed vrstvy 2 ne mezi 00–08.

### 8.3 ToyShaders = ShaderGen v2 (MirrorBooth)

**Plány (na `main`, PR #11 mergnut 29. 9. z větve `claude/opus-toyshader-pipeline-plan-8d8qh1`, 27. 9.):** `Prompts/07-PLAN-shadertoy-pipeline.md` (fázovaný implementační plán, 8 fází = 8 PR) a `Prompts/07-SETUP-shadertoy-pipeline-infra.md` (runbook SPARK/M2/JODA). **Předchůdce:** `Prompts/07-PLAN-filters-shaders-pipeline.md` + scaffolding na větvi `claude/plan-shader-pipeline-filters-axG6R` (červen: vlastní CC0 seed korpus, deterministický adaptér, integrator se sentinely, 18 shaderů, testy; merge bez konfliktů, ale mění i release workflow a pbxproj) — v2 ho nahrazuje; co z něj převzít pro Fáze 3 a 6, rozhodne Fáze 0, ne slepý merge.

**Cíl v2:** harvest ze **Shadertoy API** (klíč `SHADERTOY_API_KEY` si vytvoří uživatel) → **licenční filtr** (výchozí CC BY-NC-SA = jen RAG inspirace; přímý port jen MIT/CC0/PD/CC BY/BSD, povinná atribuční hlavička, `integrate.py` odmítne `license_ok=false` — nesmí obejít žádný přepínač) → režim *port* (deterministický transpiler podle tabulky kontraktu + `llm_fixer` jen na vyjmenované opravy) nebo *inspire* (dnešní graf + Shadertoy RAG s atribucí) → validace **`impellerc`** pro Metal/GLES/Vulkan + rozpočet výkonu → **headless náhled** (`moderngl`, 3 časy, metriky) + **vizuální ranker** → `integrate.py` (markery `@shadergen:*` v `mirror_filter.dart` a pubspec, `flutter analyze && flutter test`, rollback dotčených souborů). Uniform kontrakt aplikace (`uResolution → [uTime] → [uFaceCenter, uFaceScale]`, jediný `uTexture`) se **nemění**; `iMouse` → `uFaceCenter`.

**Fáze (každá = PR + zelené CI, pořadí závazné):** 0 tooling + `llm.py` (role architect/coder/ranker/fixer/vision) + opravy `state`/`validator` + CI job `pipeline-tests` · 1 Shadertoy klient, licence, portability, `harvest.py` · 2 RAG ingest s atribucí (kolekce `shadertoy_shaders`) · 3 transpiler + port režim + fixer · 4 validator v2 (`impellerc`, perf) · 5 headless preview + vision ranker · 6 markery v appce + `shader_contract_test.dart` + `integrate.py` · 7 batch + README. **DoD:** 3 nové filtry (1 port, 2 inspire) ověřené uživatelem na zařízení.

**Kde se v2 potkává s tímto benchmarkem (zapracovat ve Fázi 0):**
- SETUP §1.1 předpokládá `:8000/v1` a navrhuje stáhnout Qwen2.5-Coder-32B / Llama-3.3-70B 4-bit / DeepSeek-Coder. U nás je LiteLLM `:8080` s aliasy → `SPARK_BASE_URL=http://192.168.88.66:8080/v1`, `SPARK_MODEL=<vítěz B1+B5>`. Qwen2.5-Coder na disku není; Llama-3.3-70B máme jen fp8 a je pomalá (§1). **Nový model se nestahuje, dokud bench neřekne, že žádný rezidentní nestačí.** Rozhodnutí C plánu (provider `anthropic` pro vizi) zůstává jen jako záloha.
- SETUP §1.2 vision, cesta 1 = naše `docker-compose.vl.yaml` (Qwen2.5-VL-7B přes LiteLLM): `SPARK_VISION_BASE_URL=http://192.168.88.66:8080/v1`, `SPARK_VISION_MODEL=vl`; `llm.py` role `vision` i pro providera `spark` (SETUP to sám požaduje).
- Bench B (§3) bere artefakty v2: B2 = Fáze 4, B3 = Fáze 5 `preview/render.py`, B4 = Fáze 5 `vision_ranker`. Coder role se rozhodne hned (B1, B5, B2 ručně); VL až po Fázi 5.
- JODA je v SETUP volitelná (dávky, headless GL) — **nezapojovat**, M2 + SPARK stačí (JODA má swap 0,76 GB a nechceme ji zatěžovat).
- Shadertoy API je z cloudu 403 → harvest a živé testy jen z Claude Code CLI na M2.

**Předpoklady na uživateli (SETUP §2, §4):** Shadertoy App Key (`shadertoy.com/myapps`); Flutter 3.41.x + `flutter precache --ios --macos` (impellerc); `brew install python@3.11 glslang jq`; vlastní testovací selfie do `pipeline/assets/test_input/` (repo má jen placeholder); telefon pro QA; před začátkem `flutter run -d ios` na nezměněném `main`.

**Okno:** M2 kdykoli; SPARK jen LLM přes LiteLLM (den qwen36/translate/Nano, noc director) + VL (den 1 večer, den 2). Žádný GPU render na SPARKu.

**Stav:** dokumentace na `main`; implementace Fáze 0 nezačala; scaffolding z června na větvi.

---

## 9. Stavová tabule (jediné místo, kde se odškrtává)

| # | Krok | Kdo | Stav |
|---|---|---|---|
| 0 | Souhlasy z §7.1 (audio, noc 1 swarm profil, noc 2 bez obohacení, glslang, hodnocení, vlny Labu) | uživatel | čeká |
| 1 | `bench/` harness, sady, aliasy `bench-*`/`llama33`, `docker-compose.vl.yaml` | Claude | hotovo (větev `feat/model-bench`) |
| 2 | Lab příprava bez GPU (§8.1) na `feat/lab-storyteller-characters` | Claude | – |
| 3 | ToyShaders v2: Fáze 0 (tooling, `llm.py` s rolí `vision` pro `spark`, config → `:8080`) → Fáze 1–2 (harvest, RAG); B1/B5 na dnešním grafu hned | Claude (+ uživatel: Shadertoy klíč, Flutter 3.41, glslang, selfie) | – |
| 4 | Den 1: qwen36 → translate → Nano+VL; Lab vlna A, B(8) | Claude + uživatel (vlny) | – |
| 5 | Noc 1: director swarm profil; C1 | Claude | – |
| 6 | Den 2: Gemma; Lab B zbytek, C, D, E | | – |
| 7 | Noc 2: Llama-3.3-70B, gpt-oss | | – |
| 8 | §6 vyplněná; rozhodnutí Právník (chat/agent), ShaderGen (model/VL), Lab tabulka | | §6a vyplněno, čeká rozhodnutí |
| 9 | Navazující: `pravnik` merge + tunel; MirrorBooth `visual_ranker`; storyteller `models_styles.sql` + `ref2img` | | – |
