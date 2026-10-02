# services/audio

Generování hudby, SFX a řeči (TTS) lokálně na SPARKu. Nahrazuje ElevenLabs
v Kirian pipeline a dává hlasy MemeShorts; API je provider-agnostické, takže
klient nikdy nevidí jméno modelu (kromě `/v1/audio/models`, kde je to smysl).

## Z čeho se to skládá

```
audio                 :8093   orchestrátor — fronta, ffmpeg post-proc, SQLite, katalog licencí
audio-music           :8094   ACE-Step 1.5 REST API (upstream server, vlastní image pro sm_121)
audio-sfx             :8095   MOSS-SoundEffect v2.0 / Stable Audio Open + vlastní wrapper
audio-tts             :8102   Kokoro-82M + Piper — CPU (onnxruntime), bez GPU
audio-tts-xtts        :8103   XTTS-v2 — GPU, NEKOMERČNÍ licence
audio-tts-chatterbox  :8104   Chatterbox Multilingual (+ český T3) — GPU
```

Orchestrátor nemá torch, ale od vibe má numpy + librosu (tempo a tónina
předlohy) — image vyrostl z 0,6 na 1,1 GB, rebuild pořád v minutách.

Plán počítal s jedním kontejnerem a dvěma líně načítanými backendy. Rozdělené
je to proto, že modelové runtime se nesnesou v jednom image (ACE-Step chce
`transformers<4.58` a nano-vllm, MOSS jiné pinu), a hlavně proto, že takhle
smí controller shodit model bez toho, aby služba přišla o rozdělané joby.

## Odchylky od plánu (a proč)

| plán | skutečnost | důvod |
|---|---|---|
| váhy v `/opt/audio` | `~/dev/audio` (`AUDIO_MODELS_PATH`) | na SPARKu není passwordless sudo |
| jeden kontejner, dva líné backendy | tři kontejnery | ACE-Step a MOSS mají neslučitelné piny (`transformers`, `numpy`, `diffusers`); navíc takhle smí controller shodit model, aniž by služba přišla o rozdělané joby |
| dlouhé generace přes gen-queue (:8091) | vlastní fronta v `audio` | gen-queue je psaná na FLUX NIM a UGC pipeline; dvě fronty nad sebou jen znásobí místa, kde se job ztratí. Kontrakt (`202 {job_id}` → poll → výsledek) je stejný. |
| Stable Audio Open jako primární SFX | MOSS-SoundEffect v2.0 | Apache-2.0 bez stropu na obrat proti Stability Community s limitem 1 M USD, a MOSS není gated |
| MMAudio „ověřit licenci" | vyřazeno | váhy jsou CC-BY-NC-4.0 (kód MIT), pro Steam nepoužitelné |
| push image do registry na JODA | image zůstávají na SPARKu | žádné registry na JODA neběží a image se spouštějí tam, kde se buildí |
| priorita modelů v controlleru | jen pravidlo v dokumentaci | controller dnes žádnou evikci podle paměti nemá; přidávat ji je samostatná změna |

## API

```
POST /v1/audio/music   {prompt, duration_s, seed?, lyrics?, instrumental, bpm?, key?, loop, format, variations}
POST /v1/audio/sfx     {prompt, duration_s, seed?, variations, mono, format}
                       → 202 {job_id, queue_position}
GET  /v1/audio/jobs/{id}                      → {status, outputs:[{url, duration, loudness_lufs, seed, sha256}]}
GET  /v1/audio/jobs/{id}/outputs/{filename}   → audio
GET  /v1/audio/models                         → modely + licence + dostupnost
GET  /v1/audio/models/excluded                → co je vyřazené a proč
POST /v1/audio/models/{name}/load|unload
```

Vibe z předlohy — nová skladba se zvukem a náladou nahraného samplu
(ACE-Step 1.5; měření a ověřené API v `NOTES.md`):

```
POST /v1/audio/vibe/samples    multipart sample=@…    → {sample_id, duration_s, …, analysis?}
POST /v1/audio/vibe/analyze    {sample_id}            → 202 {job_id}; result = caption, bpm, keyscale, …
POST /v1/audio/vibe/generate   {sample_id, mode: vibe|groove, caption?, user_hint?, bpm?, keyscale?,
                                duration_s?, cover_strength?, lm_plan?, variations, seed?, format}
                                                      → 202 {job_id}; result = manifest, outputs mp3 + alt.wav
```

- **vibe** — text2music s referencí (přesně 30 s předlohy): nová melodie, stejná barva a tempo
- **groove** — cover předlohy (síla 0.5): drží rytmus a formu, mění kabát

Dvoufázově záměrně: aplikace ukáže, co LM z předlohy „slyšel", uživatel
opraví caption a teprve pak generuje. Analýza je taky job (jde přes frontu
hudby a přes Cloudflare by synchronně nestihla 100 s).

```bash
curl -F sample=@lofi.mp3 spark:8093/v1/audio/vibe/samples
python3 services/audio/scripts/vibe.py lofi.mp3 --mode vibe --batch 3 --hint "more cinematic"
```

ElevenLabs shim pro přechodové období (blokuje do dokončení, vrací audio):

```
POST /v1/sound-generation   {text, duration_seconds}
POST /v1/music/compose      {prompt, music_length_ms}
```

Přes gateway je všechno pod `llm.ol1n.com` se stejnou autentizací jako ostatní
služby; přímý přístup po LAN je `spark:8093`.

## TTS — řeč (MemeShorts)

Čtyři enginy ve třech kontejnerech. Kokoro a Piper jsou ONNX a jedou na CPU,
takže `audio-tts` smí běžet pořád; XTTS-v2 a Chatterbox potřebují GPU a mají
každý vlastní image (Chatterbox chce transformers 5.2, coqui-tts 4.57).

| engine | modely | jazyky | klon | licence | kontejner |
|---|---|---|---|---|---|
| kokoro | `kokoro-82m` (54 hlasů) | en, es, fr, hi, it, ja, pt, zh — **bez češtiny** | ne | Apache-2.0 ✅ | audio-tts (CPU) |
| piper | `piper-cs-kasandra-medium` | cs | ne | CC BY 4.0 ✅ (uvést autora) | audio-tts (CPU) |
| piper | `piper-cs-jirka-medium`, `-low` | cs | ne | neověřené ❌ (z lessac) | audio-tts (CPU) |
| xtts | `xtts-v2` (58 vestavěných hlasů) | 17 vč. cs | ano | **CPML — nekomerční** ❌ | audio-tts-xtts (GPU) |
| chatterbox | `chatterbox-multilingual` (V3) | 23 — **bez češtiny** | ano | MIT ✅ + vodoznak Perth | audio-tts-chatterbox (GPU) |
| chatterbox | `chatterbox-cs` (gated, volitelný) | cs | ano | neověřené ❌ | audio-tts-chatterbox (GPU) |

Podrobnosti a zdroje licencí: `LICENSES.md`.

```
POST /v1/audio/tts      {text, language, voice?, engine?, model?, commercial_only=true,
                         speed, seed?, variations, format=wav, params{exaggeration, cfg_weight,
                         temperature, top_p, repetition_penalty}}
                         → 202 {job_id}; výsledek = WAV/MP3 mono 48 kHz, −16 LUFS, manifest s licencí
GET  /v1/audio/voices   ?engine=&language=&commercial=&type=preset|builtin|custom
POST /v1/audio/voices   multipart voice_id, sample, rights, source, name?, language?, gender?, replace?
GET|DELETE /v1/audio/voices/{id}      GET /v1/audio/voices/{id}/sample
GET  /v1/audio/models?kind=tts&commercial=true
POST /v1/audio/speech   OpenAI kompatibilní shim (blokuje, vrací audio)
```

Výběr enginu (`app/tts/resolve.py`), když ho klient neurčí:

- hlas s prefixem (`kokoro:am_puck`, `piper:cs_CZ-…`, `xtts:Ana Florence`,
  `chatterbox:default`, `custom:smug-cat`) určí engine; bez prefixu se hledá
  uložený hlas, pak preset Kokoro/Piper;
- **uložený hlas** (klon) → Chatterbox, pro češtinu chatterbox-cs, nakonec XTTS;
- bez hlasu podle jazyka: Kokoro → Piper (čeština) → Chatterbox → XTTS;
- `commercial_only` (výchozí **true**) přeskočí všechno nekomerční a neověřené;
  explicitně vyžádaný takový model vrátí **403** s vysvětlením. Na pokusy
  `commercial_only=false`.

Hlas postavy se nahraje jednou a pak se na něj odkazuje jménem:

```bash
curl -F voice_id=smug-cat -F sample=@smug.wav -F rights=own -F source="já, 2026-10" \
     spark:8093/v1/audio/voices
curl -H 'content-type: application/json' spark:8093/v1/audio/tts \
     -d '{"text":"You call that a fix?","language":"en","voice":"smug-cat"}'
```

Vzorek se při uploadu ořízne na nejvýš 30 s, převede na mono 24 kHz a
normalizuje (Chatterbox z něj stejně bere jen prvních 10 s — 10–20 s čisté
řeči je ideál). `rights` a `source` jsou povinné a jdou do manifestu.

Když GPU engine neběží (v profilu, kde na něj není paměť), `/v1/audio/tts`
vrátí hned **503** — ne job, který by spadl po frontě. Kokoro a Piper jsou
nahoře pořád.

Pasti:

- `hf download` s vyjmenovanými soubory **tiše ignoruje `--include`** —
  `download.sh` proto Kokoro stahuje dvěma voláními.
- coqui-tts od torch 2.9 při importu **vyžaduje nainstalovaný torchcodec**
  (jinak `ImportError`), přestože ho XTTS při inferenci nepotřebuje. Referenci
  server čte přes soundfile (`_patch_xtts_audio_loader`), torchcodec je v image
  jen kvůli té kontrole.
- `Xtts.eval()` vrací `None` (přetěžuje `nn.Module.eval`) — `model.to().eval()`
  v řetězu dá `None` místo modelu.
- Chatterbox neumí `speed` — parametr se u něj ignoruje.
- Chatterbox generuje nejvýš 1000 speech tokenů (~40 s); server delší text dělí
  po větách (`TTS_CHUNK_CHARS`) a skládá s 120 ms pauzou.

## Post-processing

Každý výstup projde ffmpeg řetězem, protože model vrací WAV neurčité hlasitosti
s tichem na krajích:

- **trim** ticha (jen SFX, práh −50 dB peak)
- **loopify** (jen hudba s `loop=true`) — ocas se prolne přes hlavu, výstup je
  o délku prolnutí kratší a konec navazuje na začátek bez lupnutí
- **normalizace** na −16 LUFS (hudba) / −18 LUFS (SFX), true peak −1 dBTP
- **převod** na OGG Vorbis `-q 6`, SFX mono, 44.1 kHz

TTS: ořez ticha, mono, **48 kHz** (vzorkovací frekvence videa), −16 LUFS,
výchozí formát WAV (MemeShorts skládá audio ffmpegem a ztrátový mezikrok by
jen přidal artefakty).

Normalizace je jedno měření + posun hlasitosti a limiter, ne dvouprůchodový
`loudnorm`: ten mění dynamiku a na půlsekundovém SFX si s gatováním neporadí,
kdežto transient je u herních SFX to jediné, na čem záleží.

## Nasazení na SPARK

Stack se nasazuje `git pull` v `/home/ol1n/deploy/AiStack` — image se buildí
na místě, protože musí být aarch64. Build z Macu nefunguje.

```bash
ssh spark
cd ~/deploy/AiStack && git pull
make download-audio     # jen poprvé
make build-audio        # ~40 min: uv stahuje torch cu130 a nvidia knihovny
make up-audio
```

## Nasazení TTS na SPARK

**Jen v bezpečném okně** (profil llm 19–01, nebo 07–13 s `MemAvailable ≥ 20 GiB`).
Ve director oknech (01–07, 13–19) je volných 4–8 GiB a build i model by shodil
director přes OOM guard (`PLAN-spark-scheduler.md` §3b).

```bash
ssh spark
cd ~/deploy/AiStack && git pull
free -g                          # MemAvailable ≥ 20 GiB, jinak nepokračovat
make download-audio-tts          # ~12 GB do $AUDIO_MODELS_PATH (Kokoro, Piper cs, XTTS, Chatterbox)
make build-audio-tts             # orchestrátor + CPU TTS, minuty
make up-audio-tts                # znovu vytvoří `audio` s novým image + spustí audio-tts
make smoke-audio-tts             # EN Kokoro, CZ Piper → bench/smoke_tts/*.wav
make build-audio-tts-gpu         # XTTS + Chatterbox image, ~20–40 min, ~10 GB každý
make up-audio-tts-chatterbox     # GPU — jen v profilu s volnou pamětí
make up-audio-tts-xtts
python3 services/audio/scripts/smoke_tts.py --gpu --ref postava.wav
make stop-audio-tts-gpu          # docker stop, ne compose down
```

## Provoz

```bash
make download-audio     # ~30 GB do $AUDIO_MODELS_PATH
make build-audio
make up-audio           # nebo up-audio-music / up-audio-sfx zvlášť
make logs-audio

python3 services/audio/scripts/smoke_music.py
python3 services/audio/scripts/smoke_sfx.py
python3 services/audio/scripts/bench.py      # → bench/timings.csv
```

Modely žijí mimo repo v `$AUDIO_MODELS_PATH` (default `/home/ol1n/dev/audio/models`).
Licence viz `LICENSES.md`.

## Naměřený výkon (GB10)

| | čas |
|---|---|
| hudba, 30s zadání → hotový OGG | 12–15 s |
| vibe: analýza + 2 varianty ~25 s | ~30 s |
| SFX, MOSS na 100 krocích | ~24 s |
| první SFX po startu kontejneru | +60 s (torch.compile) |
| první hudba po startu kontejneru | +8 min (natažení 10 GB vah) |

SFX škáluje lineárně s počtem kroků (`SFX_DEFAULT_STEPS`): 100 → 22,5 s,
50 → 11,2 s, 30 → 6,7 s. Sto je doporučení autorů modelu, proto je výchozí.

### TTS (naměřeno 2026-10-02 na Macu v Dockeru linux/arm64, 4 jádra; SPARK zatím neměřen)

| | čas | paměť |
|---|---|---|
| Kokoro, věta ~4 s řeči | 1,4–2,7 s (RTF 0,4–0,7) | audio-tts celkem ~1 GiB |
| Piper kasandra, ~6 s řeči | 0,3 s samotná syntéza | (v tom) |
| XTTS-v2 na CPU, věta, vestavěný hlas | 13 s (RTF ~2) | ~3,1 GiB RSS |
| XTTS-v2 na CPU, klon, ~7,5 s řeči | 25 s | |
| Chatterbox V3 na CPU, 2,8 s řeči | ~10 min (S3Gen na CPU) | ~4,9 GiB RSS |

Na GPU se XTTS i Chatterbox čekají o řád rychlejší — změřit po nasazení
(`scripts/smoke_tts.py --gpu`).

## Známá rizika na GB10 / sm_121

- **CUDA knihovny: pip musí být před systémem.** Base image veze CUDA 13.0.2
  v `/usr/local/cuda`, torch wheel svoje v `site-packages/nvidia`. Bez
  `LD_LIBRARY_PATH` vyhraje ldconfig a i triviální `a @ a` na GPU skončí na
  `CUBLAS_STATUS_INVALID_VALUE`. Vypadá to jako nepodporované sm_121, ale
  není — se správnou knihovnou tentýž matmul projde. Oba runtime image to mají
  nastavené a build to kontroluje.
- **ACE-Step chce váhy na `/app/checkpoints`.** `ACESTEP_CHECKPOINTS_DIR` sice
  existuje, ale ne každá větev inicializace ji čte; s vahami jinde si server
  beze slova stáhl 10 GB znovu z ModelScope rychlostí 300 kB/s.
- `torch.compile` u SFX na GB10 **funguje** a je skoro 2× rychlejší (30 kroků:
  12,5 s → 6,7 s), proto je v `audio-sfx` zapnutý. U ACE-Stepu ověřený není a
  zůstává vypnutý — hudba je i tak rychlá.
- flash-attn / xformers pro sm_121 nemají prebuilt wheel — obojí se
  neinstaluje, jede se na SDPA.
- `audio-music` má checkpoint namountovaný **zapisovatelně**. ACE-Step si při
  inicializaci srovnává `.py` soubory v adresáři s vahami proti kódu v repu a
  při neshodě je přepíše; pod `:ro` mountem to shodí start, ne generaci, takže
  by to vypadalo na rozbitý model. `audio-sfx` má `/models` dál jen ke čtení.
- Audio modely stojí vedle vLLM v jednom 128GB poolu. `audio-sfx` má
  `SFX_IDLE_UNLOAD_S=900`; při tlaku na paměť shazuj audio první.
