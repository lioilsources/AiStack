# Služby AiStacku

Přesunuto z `CLAUDE.md` (zkrácení na 80 řádků). Architektura: [architecture.md](architecture.md), porty: [ports.md](ports.md).

## Image generation — gen-queue (`/nim/*`)

`services/gen-queue/` (Go) je robustní async job queue pro FLUX NIM modely.
Nahrazuje původní Python `nim-kontext-proxy`. Cloudflare routuje
`llm.ol1n.com/nim/*` přímo na `gen-queue:8091` (mimo hlavní gateway).

- `POST /nim/{flux-schnell|flux-kontext}/v1/infer` → `202 {id, queue_position}`
- `GET  /nim/{model}/jobs/{id}`        → `{status: queued|running|done|error}`
- `GET  /nim/{model}/jobs/{id}/result` → PNG bytes
- `GET  /health`                       → `{"status":"ok","service":"gen-queue"}`

Submit vrátí job_id okamžitě (obchází CF 100s edge timeout). Worker pool volá NIM
synchronně, retry na 5xx (3 pokusy 0/5/10 s), 4xx je non-retryable. Výsledky jsou
in-memory s TTL `RESULT_TTL_SECONDS` (default 3600 s); po TTL se evictuje
**výsledek i job-status** společně → `/jobs/{id}` i `/result` pak vrací 404. Stav
je čistě in-memory — restart gen-queue ztratí všechny joby.

Kontejnery `flux-schnell` / `flux-kontext` (NIM) se spouští přes
`docker-compose.image-nim.yaml` (`make up-image-schnell` / `up-image-kontext`);
na SPARKu je `flux-schnell` doplněk brokera (`sparkctl`).
Tok requestu sleduj přes `make logs-kontext` (`[cf]` → `[queue]` → `[nim]`).

Image API nad ComfyUI (`/v1/image/*`): `services/gen-queue/IMAGE-API.md`.

## Audio generation — services/audio (`/v1/audio/*`)

Nahrazuje ElevenLabs pro Kirian. Tři kontejnery: lehký orchestrátor `audio`
(:8093, fronta + ffmpeg post-processing + SQLite) a dva modelové runtime
`audio-music` (ACE-Step 1.5, MIT) a `audio-sfx` (MOSS-SoundEffect v2.0,
Apache-2.0). Orchestrátor běží pořád; `audio-music` a `audio-sfx` jsou doplňky
brokera k profilu `comfy` (`sparkctl lease … audio-music …`). Controller je umí
zvednout taky (`/ctrl/activate?model=audio-music`, `config/models.yaml`), na SPARKu
ale rozhoduje broker.

- `POST /v1/audio/music` / `/v1/audio/sfx` → `202 {job_id}`
- `GET  /v1/audio/jobs/{id}` → stav + výstupy (délka, LUFS, seed, sha256)
- `GET  /v1/audio/models` → katalog **včetně licencí** — Kirian jde na Steam,
  takže model bez komerční licence se do produkce nesmí dostat
- ElevenLabs shim `POST /v1/sound-generation`, `POST /v1/music/compose`
  (blokující, vrací audio) — jen pro přechodové období
- Vibe z předlohy (Ol1nLLM MusicStudio): `POST /v1/audio/vibe/samples|analyze|generate`
  — ACE-Step poslechne sample a složí novou skladbu se stejnou náladou.
  Měření a pasti (náhodné vzorkování reference, LM plán nejde zopakovat):
  `services/audio/NOTES.md`

Váhy leží mimo repo v `$AUDIO_MODELS_PATH` (default `~/dev/audio/models` na SPARKu),
ne v `/opt/audio` jak říkal plán — na SPARKu není passwordless sudo.
Detaily: `services/audio/README.md`, licence `services/audio/LICENSES.md`.

### TTS (MemeShorts) — `/v1/audio/tts`, `/v1/audio/voices`

Tři další kontejnery: `audio-tts` (Kokoro-82M + Piper, **CPU**, běží pořád, ~1 GiB),
`audio-tts-xtts` (XTTS-v2, GPU) a `audio-tts-chatterbox` (Chatterbox Multilingual,
GPU). Mají compose profily `tts` / `tts-gpu`, takže `make up-audio` je nezvedne;
`make up-audio-tts`, `up-audio-tts-xtts`, `up-audio-tts-chatterbox`.

- **XTTS-v2 je NEKOMERČNÍ** (Coqui Public Model License). `/v1/audio/tts` má
  `commercial_only=true` ve výchozím stavu → nekomerční/neověřený model vrátí 403.
- **Češtinu umí jen Piper, XTTS a komunitní chatterbox-cs.** Komerčně čistá je
  jen Piper Kasandra (CC BY 4.0, uvést autora). Kokoro ani Chatterbox
  Multilingual česky neumí. Piper Jirka je dotrénovaný z lessac (Blizzard 2013,
  jen výzkum) → neověřené.
- **Řeč na mřížce**: `rhythm {bpm, style, beat}` v `/v1/audio/tts` zarovná fráze
  na doby (`app/tts/rhythm.py`). Zarovnává fráze, ne slabiky — flow to není.
- Klon hlasu postavy: `POST /v1/audio/voices` (povinné `rights` + `source`).
- GPU TTS **nikdy vedle directora**. Profily `comfy` a `llm` v katalogu brokera
  obsahují službu `tts`; GPU varianty (XTTS, Chatterbox) v katalogu
  (`services/spark-broker/deploy/config.example.yaml`) nejsou, build a testy jen
  pod `sparkctl lease` na profil s volnou pamětí.
- Detaily, pasti a nasazení: `services/audio/README.md` (sekce TTS), licence
  `services/audio/LICENSES.md`.

## Agent LLM — qwen36-agent (OpenClaw / PromoClown)

`deploy/docker-compose.agent.yaml`: nvidia/Qwen3.6-35B-A3B-NVFP4 na vLLM, v LiteLLM
jako `openclaw-default`. OpenClaw gateway běží na hostu a volá `http://127.0.0.1:8080/v1`
(gateway → litellm), protože litellm:4000 není na host publikovaný.

- **Mimo controller** — `/activate` shazuje předchozí model; agenta zvedá a shazuje
  broker jako profil `llm` (služba `qwen36`), controller ho nepřepíná.
- Tool calls `--tool-call-parser qwen3_xml` (ne hermes); thinking vypíná LiteLLM route.
- ~36 GB unified paměti (`AGENT_GPU_MEMORY_UTILIZATION=0.30`) — vLLM si je
  zabere při startu bez ohledu na zátěž a pod `util × total` volných odmítne
  nastartovat úplně.
- 121,7 GiB neuveze dva velké modely: vedle directora (0.75 = 91 GiB) ani vedle
  ComfyUI (52 GiB) se nevejde. Proto existuje jen v profilu `llm` brokera; přepnutí
  jen přes `sparkctl`, žádný ruční `docker stop`/`start`.
- `restart: unless-stopped` je schválně: zastavení vydrží, restart stroje ne.
  Profil nikdy nesmí shazovat přes `make down-agent` (`compose down`), 30. 9. 2026
  to kontejner smazalo a promo okno zůstalo bez modelu.
- Historie: když agent chyběl v seznamu kontejnerů rozvrhu (11.–16. 9. 2026), director
  dvě noci po sobě nenaběhl a ComfyUI přes den počítalo na CPU.
- První nasazení: `make download-agent` → `make up-agent` → `curl localhost:8040/v1/models`.

## spark-broker — kdo smí SPARK přepínat (`services/spark-broker`)

Fronta úloh pro všechny projekty: okno na vyžádání, jinak priorita. Sessions volají
jen jeho API (`sparkctl`), nikdy nespouštějí kontejnery samy. Profily (comfy, llm, gemma,
director, gptoss) a doplňky (flux-schnell, audio-music, audio-sfx, vl, asr,
dabing-separate) jsou v `~/spark-broker/config.yaml` na SPARKu (vzor
`services/spark-broker/deploy/config.example.yaml`). `rag-schedule.sh` zůstává jen jako
provisioner profilů volaný brokerem; pevná okna `rag-schedule.timer` neplatí. V režimu
`control` broker odmítne start, dokud timer běží. Detaily: `services/spark-broker/README.md`.
