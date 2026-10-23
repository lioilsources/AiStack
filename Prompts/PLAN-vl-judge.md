# VL soudce — druhý hodnotitel relačních kritérií (FineTuneGallery)

Brief pro autonomní Opus session. (Revize 2026-10-05: soudce = qwen36 přes
alias `judge`, ne `vl` — viz `PLAN-spark-scheduler.md`.) Cílový repo je **FineTuneGallery**
(`/Volumes/YOTTA/Dev/FineTuneGallery`), ne AiStack — tady leží jen plán.
Soudce je **qwen36** (Qwen3.6-35B-A3B NVFP4, `qwen36-agent`), který podle
`PLAN-spark-scheduler.md` běží v okně **llm 17–01** a dělá tam vidění —
Qwen2.5-VL (`vl`) v rozvrhu není. Z AiStacku jde jen alias `judge`
v `deploy/litellm_config.yaml` (qwen36, temperature 0, thinking off, bez
fallbacku); `openclaw-default` (0.6) ani `bench-qwen36` (0.2) se pro soudce
nehodí.

---

## 1. Proč

Eval harness galerie umí odpovědět „který checkpoint drží pózu" — ale jen
tak rychle, jak rychle člověk kliká. Při ~36 hodnoceních na rameno se Wilsonovy
meze sotva rozpojí (README, osa `medium`), a každý další A/B (model, medium,
translator, LoRA síla) chce znovu desítky kliků na skupinu.

VLM jako **druhý hodnotitel** to může zlevnit — ale jen u kritéria, kde
prokazatelně souhlasí s člověkem. Soudce, který řekne „up" na všechno, má při
90% base rate pózy 90% shodu a nulovou informaci. Proto:

- **Člověk zůstává ground truth.** Soudce se do `image_criteria` nikdy nezapíše.
- **κ-gate per kritérium.** VLM verdikty kritéria smí do Evalu teprve, když
  Cohenovo κ proti člověku na dostatečném vzorku projde prahem — a to
  **per verze soudce** (alias@promptVersion). Nový prompt = nová kalibrace.
- **Pozice v Evalu je vidět.** Každá buňka říká, kolik verdiktů je lidských
  a kolik od soudce; default zůstává `source=human` a dnešní chování se nemění.

Kritéria: `pose_adherence` (výstup vs. šablona pózy), `source_identity`
a `source_style` (výstup vs. img2img zdroj). Je pravděpodobné, že soudce projde
u `source_style`, možná u `pose_adherence`, a u `source_identity` (identita
obličeje) neprojde. **To je validní výsledek, ne selhání** — gate je
od toho, aby to řekl.

---

## 2. Pravidla session

- Větev `claude/vl-judge` z `main`, **jedna fáze = jeden commit** (nebo PR,
  pokud uživatel nechce jinak), zprávy česky ve stylu repa (`feat: …`).
- Každá fáze má DoD ověřitelné **na Macu bez SPARKu**: `cd server && go test ./...`
  zelené, `cd web && npm run build` projde. Gateway se ve testech fakuje
  `httptest` serverem — vzor je `server/translate_test.go` (`fakeGateway`).
- **Na SPARK ani NAS se nesahá** (žádné ssh, docker, deploy). Jediná fáze, která
  SPARK potřebuje, je F6 — tu session jen připraví jako checklist pro uživatele.
- Čti a drž idiomy kódu: `translate.go` (verze = alias@prompt, žádný fallback,
  cache podle hashe), `tagbench.go` (CLI subcommand, tabulka), `eval.go`
  (Wilson, `criterionEligible`, n/a ≠ unrated ≠ 0 %). Komentáře anglicky
  jako okolní kód, README česky.
- Existující testy (`eval_test.go`, `TestMigrationV4KeepsV3Rows` …) se nesmí
  měnit kvůli tomu, aby prošly. Když neprojdou, je chyba v novém kódu.
- Stop podmínky — zastav a zeptej se: migrace by musela měnit/mazat existující
  sloupce; zdroj referenčního obrázku nejde spolehlivě dohledat (viz F1);
  návrh vyžaduje zápis soudce do `image_criteria`.

---

## 3. Návrh

### Data (migrace v5)

```sql
CREATE TABLE criteria_judgments (
  image_id   TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  criterion  TEXT NOT NULL,
  judge      TEXT NOT NULL,            -- 'judge@judge-v1' (alias@promptVersion)
  verdict    INTEGER NOT NULL CHECK(verdict IN (-1, 0, 1)),  -- 0 = abstain
  reason     TEXT NOT NULL DEFAULT '',
  ref_key    TEXT NOT NULL,            -- 'pose:ol3' | 'image:<id>' — co soudce viděl
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (image_id, criterion, judge)
);
```

- `verdict = 0` je **abstain** („nejde posoudit") — počítá se do pokrytí, ne
  do κ ani do Evalu. Soudce, který abstainuje na 60 %, je vidět.
- Klíč obsahuje `judge`, takže v1 a v2 promptu koexistují a kalibrují se zvlášť.
- `image_criteria` beze změny.

### Soudce (`server/judge.go`)

- `Judge` po vzoru `Translator`: `gatewayURL` (sdílí `LLM_GATEWAY_URL`),
  model `JUDGE_MODEL` (default `judge`), `judgePromptVersion = "judge-v1"`,
  `Version() = model + "@" + promptVersion`. Prázdná gateway = vypnuto (503),
  gateway dole = chyba, **žádný fallback** na jiný model.
- Jeden request = jedno kritérium, dva obrázky: nejdřív **REFERENCE**, pak
  **OUTPUT**, oba explicitně pojmenované v textu. Obrázky jako `data:` URI
  JPEG, delší strana ~768 px (reuse `imgutil.go`), temperature 0.
- Prompt per kritérium s rubrikou, co je „up" a co „down", a explicitní
  povolenou odpovědí `unsure`. Výstup JSON
  `{"verdict":"up|down|unsure","reason":"…"}`; parser tolerantní k návykům
  modelu (code fence, text kolem) jako `parseTags` — neparsovatelné = chyba,
  ne abstain.
- Eligibility = `criterionEligible` z `eval.go` (stejná pravda, žádná kopie).
- Cache: existující řádek `(image, criterion, judge)` se nepřepočítává.

### κ a gate (`server/kappa.go`)

Na průniku „člověk ±1 ∧ soudce ±1" pro dané kritérium a verzi soudce:

- Cohenovo κ, pozorovaná shoda `po`, plus **per-class agreement** (shoda na
  lidských „down" zvlášť) — menšinová třída je to, co soudce musí umět.
- 95% interval κ: asymptotická SE `sqrt(po(1−po) / (n(1−pe)²))`. Gate se
  rozhoduje na **dolní mezi**, ne na bodovém odhadu — stejná filozofie jako
  Wilson v Evalu.
- Stavy:
  - `uncalibrated` — n < 40 **nebo** < 8 lidských „down" v průniku
  - `fail` — dolní mez κ < 0.40 nebo bodové κ < 0.60
  - `pass` — jinak
- Prahy jako konstanty nahoře v souboru s komentářem proč; žádná konfigurace
  přes env (gate, který jde vypnout proměnnou, není gate).

### Slepota hodnotitele

Kalibrace platí, jen když člověk hodnotí **bez znalosti verdiktu soudce**.
Detail proto verdikt soudce pro kritérium ukáže až **poté**, co ho člověk
ohodnotil (pak jako „soudce souhlasí / nesouhlasí + reason"). Bez toho by κ
rostlo samo od sebe.

### Eval

`GET /api/eval?…&source=human|judge|merged` (default `human` = dnešek beze
změny bajt po bajtu).

- `judge` — jen verdikty soudce; kritérium, jehož gate není `pass`, je v celém
  sloupci `n/a` s důvodem (`gate: uncalibrated|fail`).
- `merged` — lidský verdikt má přednost, soudce doplní neohodnocené obrázky,
  **jen pro kritéria v `pass`**.
- `evalCell` dostane `rated_human` / `rated_judge`, aby bylo z buňky vidět, z čeho
  je. CSV dostane stejné sloupce.
- Odpověď nese `judge: {version, gates: {criterion: {state, kappa, lower, n, …}}}`.

### Fronta pro člověka

Filtr galerie `judge=disagree` (člověk ≠ soudce) a `judge=unrated` (soudce
řekl, člověk ještě ne — náhodně promíchané, viz níž). Nesouhlasy jsou
nejcennější labely pro další verzi promptu.

**Výběrová past:** kalibrační vzorek nesmí vybírat soudce. Pokud se lidské
labely sbírají přes `judge=unrated`, pořadí musí být náhodné (seed), ne
„nejistotou soudce" — jinak κ měří jen snadné případy.

---

## 4. Fáze

Každá fáze: kód + testy + DoD. Pořadí závazné.

### F0 — migrace v5 + `kappa.go`
- `migV5` (tabulka výš), přidat do `migrations`.
- `cohenKappa(pairs) → {n, po, pe, kappa, lower, upper, downAgree}` a
  `gateState(...)`.
- **DoD:** test migrace v4→v5 zachová řádky (vzor `TestMigrationV4KeepsV3Rows`);
  κ testy na ručně spočítaných tabulkách: perfektní shoda (κ=1), soudce „vždy
  up" při 90 % up (κ=0 → `fail`), náhodný soudce, n=39 → `uncalibrated`,
  7 lidských down → `uncalibrated`, hraniční pass.

### F1 — reference resolver
- `refFor(imageID, criterion) → (ref_key, jpegBytes, err)`:
  `pose_adherence` → `web/public/poses/{pose_id}.png` (přes embed `webdist`
  — ověř, že tam po buildu je; galerie je dnes servíruje jako `/poses/{id}.png`),
  `source_*` → blob obrázku `source_image_id`.
- **Ověř**, jestli `nodes.source_image_id` odkazuje na `images.id` (GenImage.id)
  — schema říká „exact parent image edited". Když ne, nebo blob chybí
  (`blob_present = 0`), resolver vrací typovanou chybu a soudce to **nezapíše
  jako abstain** (soudce neviděl referenci ≠ soudce nevěděl).
- **DoD:** testy s fixture blobem a fixture pózou; chybějící póza/zdroj =
  chyba, ne panic.

### F2 — `judge.go` proti fake gateway
- Request builder, parser, cache, zápis do `criteria_judgments`.
- `POST /api/images/{id}/judge?criterion=` (synchronní, pro jedno kritérium)
  a fronta na pozadí po vzoru `Captioner` (vypnutá, když gateway prázdná).
- **DoD:** testy — request obsahuje 2 obrázky ve správném pořadí a jméno
  kritéria; tolerantní parser (fence, prefix textu, `UNSURE` velkými);
  neparsovatelná odpověď = chyba + nic nezapsáno; 5xx = chyba, žádný jiný
  model (`TestJudgeHasNoFallback`); druhé volání jde z cache (počítadlo
  requestů na fake gateway); neeligibilní obrázek = 422.

### F3 — `judgebench` CLI
- `finetune-gallery judgebench -criterion pose_adherence -n 80 -seed 1`
  po vzoru `tagbench`: vezme **náhodný** vzorek lidsky ohodnocených eligible
  obrázků, nechá soudce soudit (cache), vypíše tabulku per kritérium:

```
judge judge@judge-v1
criterion        n   abst  po     κ     95% low  down agree  gate
pose_adherence   80  4     0.91   0.63  0.44     0.75        pass
source_style     52  2     0.85   0.55  0.31     0.60        fail
source_identity  31  9     —      —     —        —           uncalibrated
```

- Plus confusion matrix a `-v` páry s `reason` u nesouhlasů.
- **DoD:** test nad seed korpusem + fake gateway s deterministickými odpověďmi
  dá očekávanou tabulku; žádná síť.

### F4 — Eval `source=`
- `source=human|judge|merged`, gate logika, `rated_human/rated_judge`, CSV,
  blok `judge` v odpovědi.
- **DoD:** `eval_test.go` beze změny zelený (default `human`); nové testy:
  `judge` u `fail` kritéria = n/a s důvodem; `merged` — lidský verdikt přebije
  opačný verdikt soudce; abstain se nepočítá do `rated`; kritérium v `pass` pod
  verzí v1 není v `pass` pod v2.

### F5 — UI
- Eval: přepínač zdroje (human / judge / merged), u hlavičky kritéria badge
  gate stavu s κ a dolní mezí, buňky ukazují podíl soudce.
- Detail: verdikt soudce **až po lidském hodnocení** kritéria (slepota), tlačítko
  „soudit" když chybí. Galerie: filtry `judge=disagree|unrated`.
- `/api/meta`: `judge: {enabled, version}` po vzoru `translatorMeta`.
- **DoD:** `npm run build` projde; ruční průchod s `go run .` proti lokální
  DB a fake gateway (malý `httptest`-like skript nebo env na prázdnou gateway
  → UI ukáže „soudce vypnut", nic nespadne).

### F6 — kalibrace na SPARKu (uživatel, ne session)
Session připraví do README sekci + checklist, nespouští:

1. SPARK, okno llm 17–01: nasadit alias `judge` (restart litellm),
   `curl …:8080/v1/models` obsahuje `judge`. Nic dalšího se nezvedá — qwen36
   v okně běží.
2. **Ověřit dva obrázky v jednom requestu** (jeden ruční `curl` s REFERENCE +
   OUTPUT). `qwen36-agent` nemá v compose `--limit-mm-per-prompt`; když vLLM
   druhý obrázek odmítne, přidat `--limit-mm-per-prompt '{"image":2}'` — to je
   restart produkčního kontejneru (PromoClown, Právník), rozhoduje uživatel.
   Ověřit i to, že NVFP4 checkpoint vision encoder opravdu má.
3. NAS: `JUDGE_MODEL=judge` (default; gateway už je v `LLM_GATEWAY_URL`), redeploy.
4. `judgebench -n 80` per kritérium → tabulka do README. Zátěž: ~240
   requestů, odhad 15–30 min sdíleného qwen36 (`max-num-seqs 4`) — souběžně
   s Právníkem/ToyShaders, žádná paměť navíc.
5. Kritéria v `pass` → Eval `source=merged` je použitelný; `fail` →
   iterace promptu (`judge-v2`) podle nesouhlasů, ne snížení prahu.

---

## 5. README (součást F5)

Nová sekce „VL soudce" pod „Eval harness": proč druhý hodnotitel, proč κ
a ne shoda (příklad „vždy up" při 90 %), gate stavy a prahy, slepota,
výběrová past, `source=`, `judgebench`, konfigurace (`JUDGE_MODEL`). Tón
a délka jako sekce „Překlad promptu".

## 6. Mimo rozsah

- Zápis soudce do `image_criteria`, „auto-accept" verdiktů, vážení soudce.
- Nová kritéria (estetika, artefakty) — soudce hodnotí jen relační kritéria,
  kde existuje reference.
- Změny v AiStacku nad alias `judge`. Výměna modelu (třeba zpět na `vl`)
  = přepsat alias = **nová verze soudce** (`JUDGE_MODEL` je součást verze) =
  nová kalibrace, žádný kód navíc.
- Per-group κ jako gate. `judgebench` ho smí vypsat jako varování (skupina
  s n ≥ 10 a výrazně nižší shodou), ale gate je per kritérium globálně.
