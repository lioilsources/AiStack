# services/audio

Generování hudby a SFX lokálně na SPARKu. Nahrazuje ElevenLabs v Kirian
pipeline; API je provider-agnostické, takže klient nikdy nevidí jméno modelu
(kromě `/v1/audio/models`, kde je to smysl).

## Z čeho se to skládá

```
audio        :8093   orchestrátor — fronta, ffmpeg post-proc, SQLite, katalog licencí
audio-music  :8094   ACE-Step 1.5 REST API (upstream server, vlastní image pro sm_121)
audio-sfx    :8095   MOSS-SoundEffect v2.0 / Stable Audio Open + vlastní wrapper
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

## Post-processing

Každý výstup projde ffmpeg řetězem, protože model vrací WAV neurčité hlasitosti
s tichem na krajích:

- **trim** ticha (jen SFX, práh −50 dB peak)
- **loopify** (jen hudba s `loop=true`) — ocas se prolne přes hlavu, výstup je
  o délku prolnutí kratší a konec navazuje na začátek bez lupnutí
- **normalizace** na −16 LUFS (hudba) / −18 LUFS (SFX), true peak −1 dBTP
- **převod** na OGG Vorbis `-q 6`, SFX mono, 44.1 kHz

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
