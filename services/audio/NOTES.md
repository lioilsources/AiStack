# Vibe z předlohy — ověřeno na SPARKu (21. 9. 2026)

Plán `ACESTEP_VIBE_PLAN.md` počítal s Python API ACE-Stepu v procesu služby.
Tady ACE-Step běží jako upstream REST server ve vlastním kontejneru
(`audio-music`), takže všechno níž jde přes jeho HTTP API — commit
`ca1e85fe9430179831e6bc6be790c332190a3866` v image `aistack/audio-music`.

## Odchylky od plánu

| plán | skutečnost | proč |
|---|---|---|
| Python API ACE-Stepu v procesu služby | REST API kontejneru `audio-music` | orchestrátor je bez torch a model smí controller shodit, aniž by služba přišla o frontu (README) |
| `POST /vibe/analyze` synchronně | upload (`/vibe/samples`, sync) + analýza jako job | analýza jde přes frontu GPU a první požadavek čeká na váhy — přes Cloudflare by nestihla 100 s |
| multipart sample u `/generate` | `sample_id` z uploadu | předloha se nahrává jednou; id je hash bajtů, reupload téhož souboru vrátí i hotovou analýzu |
| caption jako `<žánr>, <nástroje>, …` | próza z LM, jen očištěná | v tom tvaru ji DiT zná (vlastní CoT) |
| groove síla 0.2–0.4 | 0.5 | měřeno níž — pod 0.5 rytmus nedrží |
| LM plánuje strukturu (thinking) | volitelné `lm_plan`, výchozí vypnuto | nejde zopakovat, 3–4× pomalejší |
| librosa `beat_track` | `feature.tempo` | tracker u úzkopásmové předlohy vrací 0 |
| mp3 192k CBR | mp3 VBR `-q:a 2` (~190 kb/s) | stejný enkodér jako zbytek služby |
| gen-queue | vlastní fronta služby | jako u hudby a SFX (README) |
| `services/audio/{analyze,prompt,generate,postprocess,api,vibe}.py` | `app/vibe/{sample,analyze,prompt,generate,jobs}.py`, endpointy v `app/main.py`, post-processing je sdílený `app/postproc.py`, CLI `scripts/vibe.py` | zapadá do existující struktury služby |

## Co ACE-Step REST umí a jak se to volá

| potřeba | volání | poznámka |
|---|---|---|
| poslech předlohy (audio → caption/metadata) | `POST /release_task` multipart `src_audio=@…`, `full_analysis_only=true` | VAE zakóduje audio do 5Hz kódů, `llm_handler.understand_audio_from_codes()` je přečte (`api/job_analysis_runtime.py`). ~5 s na 30 s předlohy. |
| vibe | `task_type=text2music` + multipart `reference_audio=@…` | turbo referenci umí (README, sloupec „Refer audio") |
| groove | `task_type=cover` + multipart `src_audio=@…` + `audio_cover_strength` | LM se u coveru přeskočí sám |
| výsledek | `POST /query_result` | analýza přijde jako položka se `status_message: "Full Hardware Analysis Success"` a klíči `bpm, keyscale, timesignature, duration, genre, prompt, lyrics, language, audio_codes` |

Soubory se musí **uploadovat** (multipart), ne předat cestou: modelový
kontejner nevidí na disk orchestrátoru. Multipart pole jsou text, bool
ACE-Step čte z `"true"/"false"`.

`guidance_scale` a `shift` mají efekt jen u base modelu — u turba se neladí.

Tvar odpovědi analýzy (lo-fi předloha, doslova):

```
bpm 81, keyscale "E major", timesignature "4", genre "Lo-fi hip hop",
lyrics "[Instrumental]", language "unknown",
prompt "A classic lo-fi hip-hop instrumental built on a dusty, sampled drum break …"
```

Caption je souvislá próza — stejný tvar, jaký LM generuje v CoT pro DiT,
takže se nepřestavuje do seznamu tagů (plán §3 krok 2 navrhoval
`<žánr>, <nástroje>, …`). Uklízí se jen vata na začátku vět.

## Past: reference se vzorkuje náhodně

`process_reference_audio()` bere z reference tři 10s úseky s posunem
`random.randint(0, třetina − 10 s)` přes **nezaseedovaný** `random`. Posun je
nulový jen pro referenci dlouhou přesně 30 s při 48 kHz, proto služba
`ref.wav` vyrábí přesně na 1 440 000 vzorků (kratší předlohu dosmyčkuje).

## Měření — tři předlohy se známým tempem

Předlohy vygenerované ACE-Stepem se zadaným tempem a tóninou (lo-fi 82 BPM,
synthwave 112, akustická kytara 96; 25–30 s, mp3):

| předloha | LM BPM | librosa BPM | vybráno | LM tónina | librosa tónina | LM žánr |
|---|---|---|---|---|---|---|
| lo-fi (82) | 81 | 161.5 | 81 (oktávová záměna) | E major | A minor | Lo-fi hip hop |
| synthwave (112) | 115 | 112.3 | 115 | A minor | C major | Chiptune |
| kytara (96) | 94 | 95.7 | 94 | G major | G major | folk |

Tempo z LM je v ±3 % (cíl plánu ±5 %). Librosa se na lo-fi chytila
dvojnásobku, proto pravidlo „liší se o víc než 15 % → librosa" má výjimku pro
přesnou oktávu (±6 %): beat tracker volí oktávu podle apriorna kolem
120 BPM, LM odhaduje vnímané tempo, a to DiT potřebuje. Librosa se měří přes
`feature.tempo`, ne `beat.beat_track` — tracker bere onsety jako medián přes
pásma a u úzkopásmové předlohy vrátí 0; na skutečné hudbě dávají obě totéž.

Tónina: LM a librosa se shodly jen u kytary (u synthwave jde o paralelní
dur/moll se stejnými předznamenáními). Která je správně, bez poslechu
nevím — služba bere LM (plán), obě hodnoty jsou v `measured` a aplikace
tóninu nechá přepsat.

## Časy (GB10, turbo, 8 kroků)

| krok | čas |
|---|---|
| upload + výběr úseku (ffmpeg) | < 1 s |
| analýza (LM poslech + librosa) | 12–16 s přes frontu, ~5 s samotný ACE-Step |
| první librosa po startu | +14 s (import + JIT numby) — služba ji zahřeje při startu |
| vibe varianta ~25 s, bez LM plánu | 4–6 s |
| vibe varianta ~25 s, s LM plánem | 12–24 s |
| groove varianta ~28 s | 4–6 s |
| **analýza + 2 varianty vibe** | **~30 s** (cíl plánu < 60 s) |

První request po startu `audio-music` natahuje váhy (~1 min).
`audio-music` drží za běhu **12,8 GB** unified paměti (s nataženým LM), ne
4,4 GB z původního měření bez LM.

## Reprodukovatelnost — plán §4 bod 5 splněný jen napůl

Stejná předloha, stejné parametry, stejný seed, dva běhy; korelace průběhů:

| režim | korelace | |
|---|---|---|
| groove (cover) | 0.986 | prakticky tatáž stopa, rozdíl je šum GPU floatů |
| vibe bez LM plánu | 0.84 | tatáž skladba, slyšitelně jiné detaily |
| vibe s LM plánem (`thinking`) | 0.03 | jiná skladba |

LM plán nejde zopakovat, protože vllm větev (`nano-vllm`) seed do
`SamplingParams` nepředává — `torch.manual_seed(seed)` dělá jen `pt` backend
LM, a ten se volí při startu kontejneru, ne per request. Vygenerované kódy
REST API nevrací, takže je nejde ani uložit a poslat zpátky
(`audio_code_string`). Proto je `lm_plan` ve výchozím stavu vypnutý:
s ním je varianta 3–4× pomalejší a konec má 2–8 s ticha (bez něj 1–3 s),
tempo drží oba stejně. Manifest zaznamenává, *jak* stopa vznikla — slib
bitově stejné stopy to není.

## Groove — síla coveru

Plán navrhoval 0.2–0.4. Měřeno (korelace onsetové obálky s předlohou,
±0.5 s posun; průměr dvou seedů):

| síla | lo-fi | synthwave | kytara |
|---|---|---|---|
| 0.3 | 0.51 | 0.05 | 0.17 |
| 0.5 | 0.69 | 0.28 | 0.53 |
| 0.7 | 0.72 | 0.32 | 0.54 |
| 0.9 | 0.74 | 0.32 | 0.52 |

Vibe (bez coveru) pro srovnání 0.03–0.14. Pod 0.5 groove rytmus předlohy
drží jen někdy, nad 0.5 už nepřibývá — výchozí je proto **0.5**.
Barvu zvuku ani to, jestli „kabát" zní jinak, tahle čísla neměří; to je na
poslech.

## Co zbývá ověřit poslechem

- vibe: sedí nástroje a nálada (plán §4 bod 2) — MFCC průměr se od varianty
  bez reference liší málo (0.98 vs 0.97), takže o převzetí barvy čísla
  nerozhodnou
- groove 0.5: je kabát dost jiný, nebo je to už skoro kopie
- LM plán: stojí zlepšení struktury za ztrátu opakovatelnosti

Nástroje: `scripts/vibe.py` (akceptační běh, `--repeat` pro determinismus),
`python -m app.vibe.analyze sample.mp3` (jen analýza).
