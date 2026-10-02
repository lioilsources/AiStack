"""services/audio — provider-agnostické API pro generování hudby, SFX a řeči (TTS).

Kirian ani jiný klient nesmí vědět, že pod tím jede ACE-Step nebo MOSS. Proto
tady žije jen fronta, post-processing a katalog licencí; modely běží ve svých
kontejnerech (audio-music, audio-sfx) a mluví se s nimi přes adaptéry.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .backends import AceStepBackend, Backend, BackendError, GenSpec, SfxHTTPBackend, TtsHTTPBackend, TtsSpec
from .backends.base import MODEL_DOWN
from .backends.ttshttp import tts_down_message
from .catalog import CATALOG, EXCLUDED, ModelSpec, by_kind
from .config import Config
from .jobs import LANES, Job, Runner, make_spec_from_request
from .postproc import set_ogg_quality
from .schemas import (
    JobAccepted,
    JobStatus,
    ModelInfo,
    MusicRequest,
    Output,
    SampleOut,
    SfxRequest,
    StoredVoiceOut,
    TtsRequest,
    VibeAnalyzeRequest,
    VibeRequest,
    VoiceInfo,
)
from .store import Store
from .tts import presets as tts_presets
from .tts.resolve import ResolveError, TtsPlan
from .tts.resolve import resolve as resolve_tts
from .tts.voicestore import MAX_UPLOAD_BYTES as VOICE_MAX_BYTES
from .tts.voicestore import VoiceError, VoiceStore
from .vibe.analyze import warm_up
from .vibe.generate import build_spec, resolve
from .vibe.sample import MAX_UPLOAD_BYTES, SampleError, SampleStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("audio")

# Stav se staví až v lifespanu, ne při importu: konfigurace se čte z prostředí
# a testy potřebují každý běh v jiném adresáři.
# Anotace bez přiřazení by tu jméno nezaložila a první request před startem
# by spadl na NameError místo srozumitelné hlášky — proto explicitní None.
CONFIG: Config = Config()
store: Store = None  # type: ignore[assignment]  # nastaví lifespan
runner: Runner = None  # type: ignore[assignment]  # nastaví lifespan
samples: SampleStore = None  # type: ignore[assignment]  # nastaví lifespan
voices: VoiceStore = None  # type: ignore[assignment]  # nastaví lifespan
backends: dict[str, Backend] = {}


def _url(value: str) -> str:
    return "" if value.strip().lower() in ("off", "-", "none") else value


@asynccontextmanager
async def lifespan(_: FastAPI):
    global CONFIG, store, runner, samples, voices
    CONFIG = Config()
    set_ogg_quality(CONFIG.ogg_quality)
    Path(CONFIG.data_dir).mkdir(parents=True, exist_ok=True)

    store = Store(CONFIG.db_path)
    samples = SampleStore(Path(CONFIG.data_dir) / "vibe-samples")
    voices = VoiceStore(Path(CONFIG.data_dir) / "tts-voices")
    backends.clear()
    if CONFIG.music_url:
        backends["music"] = AceStepBackend(
            CONFIG.music_url, CONFIG.music_model, CONFIG.music_timeout_s
        )
    if CONFIG.sfx_url:
        backends["sfx"] = SfxHTTPBackend(
            CONFIG.sfx_url, CONFIG.sfx_model, CONFIG.sfx_timeout_s
        )
    tts_urls = {
        "kokoro": _url(CONFIG.tts_cpu_url),
        "piper": _url(CONFIG.tts_cpu_url),
        "xtts": _url(CONFIG.tts_xtts_url),
        "chatterbox": _url(CONFIG.tts_chatterbox_url),
    }
    if any(tts_urls.values()):
        backends["tts"] = TtsHTTPBackend(tts_urls, CONFIG.tts_timeout_s)

    orphans = store.requeue_orphans()
    if orphans:
        log.warning("%d jobů zachyceno restartem, označeno jako chybové", orphans)

    runner = Runner(CONFIG, store, backends, samples)
    runner.start()
    # První volání librosy stojí ~14 s (import + JIT numby) — ať je nezaplatí
    # první analýza, která na ně čeká uživatel v telefonu.
    threading.Thread(target=warm_up, name="vibe-warmup", daemon=True).start()
    log.info(
        "audio ready — music=%s sfx=%s tts=%s",
        CONFIG.music_url or "-", CONFIG.sfx_url or "-",
        {e: u or "-" for e, u in tts_urls.items()},
    )

    yield

    runner.stop()
    for backend in backends.values():
        close = getattr(backend, "close", None)
        if close:
            close()
    store.close()


app = FastAPI(title="AiStack audio", version="1.0.0", lifespan=lifespan)


# --- auth ---


def require_key(
    authorization: str | None = Header(default=None),
    xi_api_key: str | None = Header(default=None, alias="xi-api-key"),
) -> None:
    """Volitelný klíč. Prázdný AUDIO_API_KEY = služba jede jen v interní síti."""
    if not CONFIG.api_key:
        return
    presented = xi_api_key or ""
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:]
    if not secrets.compare_digest(presented, CONFIG.api_key):
        raise HTTPException(status_code=401, detail="neplatný klíč")


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


# --- health & katalog ---


@app.get("/health")
def health() -> dict[str, Any]:
    # Doba nečinnosti na kategorii je tu proto, aby se controller mohl
    # rozhodnout, jestli smí shodit modelový kontejner, aniž by přerušil job.
    return {
        "status": "ok",
        "service": "audio",
        "backends": sorted(backends),
        "idle_s": {lane: _round_or_none(runner.idle_seconds(lane)) for lane in LANES}
        if runner is not None
        else {},
    }


def _model_info(spec: ModelSpec, *, available: bool, loaded: bool, detail: str) -> ModelInfo:
    return ModelInfo(
        name=spec.name,
        kind=spec.kind,
        backend=spec.backend,
        license=spec.license,
        license_url=spec.license_url,
        commercial=spec.commercial,
        note=spec.note,
        upstream=spec.repo,
        available=available,
        loaded=loaded,
        detail=detail,
        license_status=spec.license_status,
        attribution=spec.attribution,
        languages=list(spec.languages),
        cloning=spec.cloning,
        watermark=spec.watermark,
    )


@app.get("/v1/audio/models", response_model=list[ModelInfo])
def list_models(
    kind: str | None = Query(default=None, pattern="^(music|sfx|tts)$"),
    commercial: bool | None = None,
    _: None = Depends(require_key),
) -> list[ModelInfo]:
    """Katalog s licencemi. `?commercial=true` vrátí jen komerčně použitelné modely."""
    infos: list[ModelInfo] = []
    for k in ("music", "sfx"):
        if kind and kind != k:
            continue
        backend = backends.get(k)
        backend_ok, detail = backend.health() if backend else (False, "backend není nakonfigurován")
        loaded = set(backend.loaded_models()) if backend and backend_ok else set()
        for spec in by_kind(k):
            served_detail = detail
            if not spec.served:
                served_detail = "žádný runtime tenhle model neobsluhuje — viz poznámka"
            infos.append(_model_info(
                spec, available=backend_ok and spec.served, loaded=spec.name in loaded, detail=served_detail,
            ))
    if kind in (None, "tts"):
        infos += _tts_model_infos()
    if commercial is not None:
        infos = [m for m in infos if m.commercial == commercial]
    return infos


def _tts_model_infos() -> list[ModelInfo]:
    backend: TtsHTTPBackend | None = backends.get("tts")  # type: ignore[assignment]
    state: dict[str, tuple[bool, str, dict[str, list[str]]]] = {}
    infos = []
    for spec in by_kind("tts"):
        engine = spec.backend
        if engine not in state:
            if backend is None:
                state[engine] = (False, "TTS backend není nakonfigurován", {"available": [], "loaded": []})
            else:
                ok, detail = backend.engine_health(engine)
                models = backend.engine_models(engine) if ok else {"available": [], "loaded": []}
                state[engine] = (ok, detail, models)
        ok, detail, models = state[engine]
        has_weights = spec.name in models["available"]
        if ok and not has_weights:
            detail = "runtime běží, ale váhy nejsou stažené — make download-audio-tts"
        infos.append(_model_info(
            spec, available=ok and has_weights, loaded=spec.name in models["loaded"], detail=detail,
        ))
    return infos


@app.get("/v1/audio/models/excluded")
def list_excluded(_: None = Depends(require_key)) -> dict[str, str]:
    """Modely vyřazené kvůli licenci — aby se na ně nikdo neptal podruhé."""
    return EXCLUDED


@app.post("/v1/audio/models/{name}/load")
def load_model(name: str, _: None = Depends(require_key)) -> dict[str, str]:
    return _model_action(name, "load")


@app.post("/v1/audio/models/{name}/unload")
def unload_model(name: str, _: None = Depends(require_key)) -> dict[str, str]:
    return _model_action(name, "unload")


def _model_action(name: str, action: str) -> dict[str, str]:
    spec = CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"neznámý model {name!r}")
    backend = backends.get(spec.kind)
    fn = getattr(backend, action, None) if backend else None
    if fn is None:
        raise HTTPException(
            status_code=501,
            detail=f"backend {spec.backend} neumí {action} za běhu — použij controller (/ctrl)",
        )
    try:
        if spec.kind == "tts":
            fn(name, spec.backend)
        else:
            fn(name)
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"model": name, "action": action, "status": "ok"}


# --- generování ---


@app.post("/v1/audio/music", status_code=202, response_model=JobAccepted)
def create_music(req: MusicRequest, _: None = Depends(require_key)) -> JobAccepted:
    return _enqueue("music", req.model_dump(), loop=req.loop, mono=False)


@app.post("/v1/audio/sfx", status_code=202, response_model=JobAccepted)
def create_sfx(req: SfxRequest, _: None = Depends(require_key)) -> JobAccepted:
    return _enqueue("sfx", req.model_dump(), loop=False, mono=req.mono)


def _enqueue(kind: str, payload: dict[str, Any], *, loop: bool, mono: bool) -> JobAccepted:
    if kind not in backends:
        raise HTTPException(status_code=503, detail=f"backend pro {kind} není nakonfigurován")
    spec = make_spec_from_request(kind, payload)
    model = spec.model or (CONFIG.music_model if kind == "music" else CONFIG.sfx_model)
    job_id = store.create(kind, payload, model)
    position = runner.submit(
        Job(
            job_id=job_id,
            kind=kind,
            spec=spec,
            variations=int(payload.get("variations", 1)),
            fmt=payload.get("format", "ogg"),
            mono=mono,
            loop=loop,
        )
    )
    return JobAccepted(job_id=job_id, queue_position=position)


@app.get("/v1/audio/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, _: None = Depends(require_key)) -> JobStatus:
    row = store.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="neznámý job")
    return _to_status(row)


@app.get("/v1/audio/jobs")
def recent_jobs(limit: int = 50, _: None = Depends(require_key)) -> list[JobStatus]:
    return [_to_status(row) for row in store.recent(limit)]


@app.get("/v1/audio/jobs/{job_id}/outputs/{filename}")
def job_output(job_id: str, filename: str, _: None = Depends(require_key)) -> FileResponse:
    row = store.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="neznámý job")
    known = {o.get("filename") for o in row["outputs"]}
    known |= {a.get("filename") for o in row["outputs"] for a in (o.get("alt") or {}).values()}
    if filename not in known:
        raise HTTPException(status_code=404, detail="neznámý výstup")
    path = Path(CONFIG.data_dir) / job_id / filename
    if not path.is_file():
        raise HTTPException(status_code=410, detail="soubor už na disku není")
    return FileResponse(path, media_type=_media_type(filename))


def _to_status(row: dict[str, Any]) -> JobStatus:
    position = None
    if row["status"] == "queued":
        position = runner.queue_position(row["job_id"], row["kind"])
    return JobStatus(
        job_id=row["job_id"],
        kind=row["kind"],
        status=row["status"],
        model=row["model"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        queue_position=position,
        error=row["error"],
        outputs=[Output(**o) for o in row["outputs"]],
        task=row.get("task") or "generate",
        result=row.get("result"),
    )


def _media_type(filename: str) -> str:
    return {
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


# --- vibe z předlohy ---
# Dvoufázově záměrně (plán vibe §3 krok 5): uživatel nejdřív vidí, co LM
# z předlohy „slyšel", opraví caption, a teprve pak generuje. Analýza je taky
# job, ne synchronní volání: jde přes frontu hudby (sdílí GPU s generováním)
# a první požadavek po startu modelu čeká na natažení vah — přes Cloudflare
# by to spadlo na 100s timeoutu.


@app.post("/v1/audio/vibe/samples", response_model=SampleOut)
def upload_sample(sample: UploadFile = File(...), _: None = Depends(require_key)) -> SampleOut:
    # Synchronně schválně: dekódování a výběr úseku je ffmpeg na sekundy
    # a v async handleru by po tu dobu stál event loop — i polly ostatních jobů.
    data = sample.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        info = samples.ingest(data, sample.filename or "sample")
    except SampleError as exc:
        status = 413 if len(data) > MAX_UPLOAD_BYTES else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return SampleOut(**info.__dict__, analysis=samples.analysis(info.sample_id))


@app.get("/v1/audio/vibe/samples/{sample_id}", response_model=SampleOut)
def get_sample(sample_id: str, _: None = Depends(require_key)) -> SampleOut:
    info = samples.get(sample_id)
    if info is None:
        raise HTTPException(status_code=404, detail="neznámá předloha")
    return SampleOut(**info.__dict__, analysis=samples.analysis(sample_id))


def _require_music_model() -> None:
    """Vibe potřebuje běžící model — jinak 503 hned, ne job, který spadne.

    Upload předlohy model nepotřebuje, analýza a skládání ano. Kontejner
    přes noc vypíná plánovač, a telefon má dostat srozumitelný důvod.
    """
    backend = backends.get("music")
    if backend is None:
        raise HTTPException(status_code=503, detail="backend pro hudbu není nakonfigurován")
    ok, detail = backend.health()
    if not ok:
        log.info("vibe: model nedostupný (%s)", detail)
        raise HTTPException(status_code=503, detail=MODEL_DOWN)


@app.post("/v1/audio/vibe/analyze", status_code=202, response_model=JobAccepted)
def vibe_analyze(req: VibeAnalyzeRequest, _: None = Depends(require_key)) -> JobAccepted:
    _require_music_model()
    if samples.get(req.sample_id) is None:
        raise HTTPException(status_code=404, detail="neznámá předloha")
    payload = req.model_dump()
    job_id = store.create("music", payload, CONFIG.music_model, task="analyze")
    position = runner.submit(
        Job(
            job_id=job_id, kind="music", spec=GenSpec(prompt="", duration_s=0.0),
            variations=0, fmt="", mono=False, loop=False, task="analyze", payload=payload,
        )
    )
    return JobAccepted(job_id=job_id, queue_position=position)


@app.post("/v1/audio/vibe/generate", status_code=202, response_model=JobAccepted)
def vibe_generate(req: VibeRequest, _: None = Depends(require_key)) -> JobAccepted:
    _require_music_model()
    info = samples.get(req.sample_id)
    if info is None:
        raise HTTPException(status_code=404, detail="neznámá předloha")
    analysis = samples.analysis(req.sample_id)
    try:
        params = resolve(req.model_dump(), analysis, info)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    model = CONFIG.music_model
    payload = {**req.model_dump(), "resolved": params.__dict__}
    job_id = store.create("music", payload, model, task="vibe")
    manifest = {
        "task": "vibe",
        "request": req.model_dump(),
        "params": params.__dict__,
        "sample": info.__dict__,
        "analysis": analysis,
        "model": model,
        # Turbo DiT, 8 kroků, ODE. Bitově stejná stopa ze stejného seedu to
        # není ani bez LM — GPU nedeterminismus rozdíl zesílí (měřeno: cover
        # korelace 0,986, vibe bez LM 0,84, s LM plánem 0,03). Manifest tedy
        # zaznamenává *jak* stopa vznikla, ne slib, že vznikne znovu stejně.
        "inference": {
            "steps": 8, "method": "ode", "lm_plan": params.lm_plan, "lufs": CONFIG.vibe_lufs,
        },
        "created_at": time.time(),
    }
    position = runner.submit(
        Job(
            job_id=job_id,
            kind="music",
            spec=build_spec(params, samples, req.sample_id, seed=req.seed, model=model),
            variations=req.variations,
            fmt=req.format,
            mono=False,
            loop=False,
            task="vibe",
            target_lufs=CONFIG.vibe_lufs,
            extra_formats=("wav",) if req.format != "wav" else (),
            manifest=manifest,
        )
    )
    return JobAccepted(job_id=job_id, queue_position=position)


# --- TTS (převod textu na řeč) ---
# MemeShorts: EN + CZ, jeden hlas na postavu, monetizované shorty. Proto je
# `commercial_only` ve výchozím stavu zapnuté a každý job nese v manifestu
# licenci, uvedení autora (CC BY) a jestli výstup nese vodoznak.


def _tts_backend() -> TtsHTTPBackend:
    backend = backends.get("tts")
    if backend is None:
        raise HTTPException(status_code=503, detail="TTS backend není nakonfigurován")
    return backend  # type: ignore[return-value]


def _plan_tts(req: TtsRequest) -> TtsPlan:
    try:
        return resolve_tts(
            language=req.language, voice=req.voice, engine=req.engine, model=req.model,
            commercial_only=req.commercial_only, voices=voices,
        )
    except ResolveError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _license_doc(spec: ModelSpec) -> dict[str, Any]:
    return {
        "model": spec.name,
        "license": spec.license,
        "license_url": spec.license_url,
        "commercial": spec.commercial,
        "license_status": spec.license_status,
        "attribution": spec.attribution,
        "watermark": spec.watermark,
        "upstream": spec.repo,
    }


@app.post("/v1/audio/tts", status_code=202, response_model=JobAccepted)
def create_tts(req: TtsRequest, _: None = Depends(require_key)) -> JobAccepted:
    plan = _plan_tts(req)
    backend = _tts_backend()
    # Model, který neběží, odmítnout hned — ne job, který spadne po frontě.
    # U GPU enginů je to běžný stav (jedou jen v některých profilech SPARKu).
    ok, detail = backend.engine_health(plan.engine)
    if not ok:
        log.info("tts: engine %s nedostupný (%s)", plan.engine, detail)
        raise HTTPException(status_code=503, detail=tts_down_message(plan.engine))

    params = req.params.model_dump(exclude_none=True)
    spec = TtsSpec(
        text=req.text.strip(),
        language=plan.language,
        engine=plan.engine,
        model=plan.model.name,
        voice=plan.voice,
        voice_kind=plan.voice_kind,
        engine_language=plan.engine_language,
        ref_path=plan.ref_path,
        speed=req.speed,
        seed=req.seed,
        params=params,
    )
    payload = req.model_dump()
    job_id = store.create("tts", payload, plan.model.name, task="tts")
    manifest = {
        "task": "tts",
        "request": payload,
        "engine": plan.engine,
        "voice": {
            "id": plan.voice,
            "type": plan.voice_kind,
            # U klonovaného hlasu jde do manifestu i prohlášení o právech.
            "stored": plan.stored.public() if plan.stored else None,
        },
        "language": plan.language,
        "license": _license_doc(plan.model),
        "lufs": CONFIG.tts_lufs,
        "sample_rate": CONFIG.tts_sample_rate,
        "created_at": time.time(),
    }
    position = runner.submit(
        Job(
            job_id=job_id,
            kind="tts",
            spec=spec,  # type: ignore[arg-type]
            variations=req.variations,
            fmt=req.format,
            mono=True,
            loop=False,
            task="tts",
            manifest=manifest,
            lane=plan.lane,
            sample_rate=CONFIG.tts_sample_rate,
        )
    )
    return JobAccepted(job_id=job_id, queue_position=position)


def _preset_infos() -> list[VoiceInfo]:
    out = []
    for p in tts_presets.presets():
        spec = CATALOG[p.model]
        out.append(VoiceInfo(
            id=f"{p.engine}:{p.voice}", engine=p.engine, model=p.model, type="preset",
            language=p.language, languages=[p.language], gender=p.gender, grade=p.grade,
            license=spec.license, commercial=spec.commercial, license_status=spec.license_status,
            attribution=spec.attribution, note=spec.note if p.engine == "piper" else "",
        ))
    return out


def _builtin_infos(backend: TtsHTTPBackend | None) -> list[VoiceInfo]:
    """Vestavěné hlasy GPU enginů — zná je jen běžící runtime."""
    if backend is None:
        return []
    out = []
    for engine in ("xtts", "chatterbox"):
        if engine not in backend.urls:
            continue
        for v in backend.engine_voices(engine):
            spec = CATALOG.get(str(v.get("model", "")))
            if spec is None or spec.kind != "tts":
                continue
            out.append(VoiceInfo(
                id=f"{engine}:{v.get('id', '')}", engine=engine, model=spec.name, type="builtin",
                languages=list(spec.languages), gender=str(v.get("gender", "")),
                license=spec.license, commercial=spec.commercial, license_status=spec.license_status,
                attribution=spec.attribution, watermark=spec.watermark,
            ))
    return out


def _custom_infos() -> list[VoiceInfo]:
    """Uložený hlas jde použít s každým klonujícím modelem — jedna položka na model."""
    out = []
    for stored in voices.list():
        for spec in by_kind("tts"):
            if not spec.cloning:
                continue
            out.append(VoiceInfo(
                id=f"custom:{stored.voice_id}", engine=spec.backend, model=spec.name, type="custom",
                language=stored.language, languages=list(spec.languages), gender=stored.gender,
                license=spec.license, commercial=spec.commercial, license_status=spec.license_status,
                attribution=spec.attribution, watermark=spec.watermark,
                note=f"{stored.name} — práva: {stored.rights}, zdroj: {stored.source}",
            ))
    return out


@app.get("/v1/audio/voices", response_model=list[VoiceInfo])
def list_voices(
    engine: str | None = None,
    language: str | None = None,
    commercial: bool | None = None,
    type: str | None = Query(default=None, pattern="^(preset|builtin|custom)$"),
    _: None = Depends(require_key),
) -> list[VoiceInfo]:
    """Všechny hlasy: presety Kokoro/Piper, vestavěné hlasy GPU enginů, uložené hlasy postav.

    `?commercial=true` = jen to, co jde do monetizovaného videa.
    """
    backend: TtsHTTPBackend | None = backends.get("tts")  # type: ignore[assignment]
    items = _preset_infos() + _builtin_infos(backend) + _custom_infos()
    if engine:
        items = [v for v in items if v.engine == engine]
    if language:
        lang = language.lower().split("-")[0].split("_")[0]
        items = [v for v in items if lang in v.languages]
    if commercial is not None:
        items = [v for v in items if v.commercial == commercial]
    if type:
        items = [v for v in items if v.type == type]
    return items


def _stored_out(voice_id: str) -> StoredVoiceOut:
    stored = voices.get(voice_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="neznámý hlas")
    return StoredVoiceOut(**stored.public(), sample_url=f"/v1/audio/voices/{voice_id}/sample")


@app.post("/v1/audio/voices", response_model=StoredVoiceOut, status_code=201)
def upload_voice(
    voice_id: str = Form(..., description="id hlasu postavy, např. smug-cat"),
    sample: UploadFile = File(..., description="čistá řeč jednoho mluvčího, ideálně 10–20 s"),
    rights: str = Form(..., description="own | consented | licensed | synthetic"),
    source: str = Form(..., description="kdo mluví a odkud vzorek je"),
    name: str = Form(default=""),
    language: str = Form(default=""),
    gender: str = Form(default=""),
    note: str = Form(default=""),
    replace: bool = Form(default=False),
    _: None = Depends(require_key),
) -> StoredVoiceOut:
    # Synchronně: ffmpeg na pár sekund, stejně jako upload předlohy u vibe.
    data = sample.file.read(VOICE_MAX_BYTES + 1)
    try:
        voices.ingest(
            voice_id, data, name=name, language=language, gender=gender,
            rights=rights, source=source, note=note, replace=replace,
        )
    except VoiceError as exc:
        status = 413 if len(data) > VOICE_MAX_BYTES else 409 if "už existuje" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _stored_out(voice_id)


@app.get("/v1/audio/voices/{voice_id}", response_model=StoredVoiceOut)
def get_voice(voice_id: str, _: None = Depends(require_key)) -> StoredVoiceOut:
    return _stored_out(voice_id)


@app.get("/v1/audio/voices/{voice_id}/sample")
def get_voice_sample(voice_id: str, _: None = Depends(require_key)) -> FileResponse:
    path = voices.ref_path(voice_id)
    if path is None:
        raise HTTPException(status_code=404, detail="neznámý hlas")
    return FileResponse(path, media_type="audio/wav")


@app.delete("/v1/audio/voices/{voice_id}")
def delete_voice(voice_id: str, _: None = Depends(require_key)) -> dict[str, str]:
    try:
        deleted = voices.delete(voice_id)
    except VoiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="neznámý hlas")
    return {"voice_id": voice_id, "status": "deleted"}


# --- OpenAI kompatibilní shim ---
# Spousta nástrojů (i ffmpeg skripty z návodů) umí jen OpenAI `/v1/audio/speech`.
# Blokuje do dokončení jobu a vrací audio, stejně jako ElevenLabs shim níž.

_OPENAI_FORMATS = {"mp3": "mp3", "wav": "wav", "flac": "flac", "opus": "ogg", "ogg": "ogg", "aac": "mp3", "pcm": "wav"}


@app.post("/v1/audio/speech")
def shim_openai_speech(body: dict[str, Any], _: None = Depends(require_key)) -> Response:
    """{model, input, voice, response_format?, speed?, language?, commercial_only?}.

    `model` = engine (kokoro, piper, xtts, chatterbox), model z katalogu, nebo
    OpenAI jméno (tts-1, gpt-4o-mini-tts …) → automatický výběr podle jazyka.
    """
    text = (body.get("input") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="chybí 'input'")
    model = str(body.get("model") or "")
    engine, model_name = "", ""
    if model in ("kokoro", "piper", "xtts", "chatterbox"):
        engine = model
    elif model in CATALOG and CATALOG[model].kind == "tts":
        model_name = model
    fmt = _OPENAI_FORMATS.get(str(body.get("response_format") or "mp3").lower(), "mp3")
    voice = str(body.get("voice") or "")
    language = str(body.get("language") or "")
    if not language:
        preset = tts_presets.piper_preset(voice.split(":")[-1]) or tts_presets.kokoro_preset(voice.split(":")[-1])
        language = preset.language if preset else "en"
    try:
        req = TtsRequest(
            text=text, language=language, voice=voice, engine=engine, model=model_name,
            speed=float(body.get("speed") or 1.0), format=fmt,
            commercial_only=bool(body.get("commercial_only", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    accepted = create_tts(req)
    row = _await_job(accepted.job_id, CONFIG.tts_timeout_s + 60)
    data, media = _first_output_bytes(row)
    return Response(content=data, media_type=media, headers={"X-Job-Id": row["job_id"]})


# --- ElevenLabs shim ---
# Přechodové období: klient přepne base URL a nemusí měnit nic jiného.
# Blokuje do dokončení jobu, protože přesně to ElevenLabs API dělá.


def _shim_format(value: Any) -> str:
    """ElevenLabs posílá 'mp3_44100_128' a spol.; nás zajímá jen kontejner."""
    text = str(value or "").lower()
    for fmt in ("ogg", "wav", "flac", "mp3"):
        if text.startswith(fmt):
            return fmt
    return "ogg"


def _await_job(job_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = store.get(job_id)
        if row is None:
            raise HTTPException(status_code=500, detail="job zmizel")
        if row["status"] == "done":
            return row
        if row["status"] == "error":
            raise HTTPException(status_code=502, detail=row["error"])
        time.sleep(1.0)
    raise HTTPException(status_code=504, detail=f"job {job_id} nedoběhl do {timeout_s:.0f}s")


def _first_output_bytes(row: dict[str, Any]) -> tuple[bytes, str]:
    output = row["outputs"][0]
    path = Path(CONFIG.data_dir) / row["job_id"] / output["filename"]
    return path.read_bytes(), _media_type(output["filename"])


@app.post("/v1/sound-generation")
def shim_sound_generation(body: dict[str, Any], _: None = Depends(require_key)) -> Response:
    """ElevenLabs Sound Effects: {text, duration_seconds, prompt_influence}."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="chybí 'text'")
    req = SfxRequest(
        prompt=text,
        duration_s=float(body.get("duration_seconds") or 2.0),
        format=_shim_format(body.get("output_format")),
    )
    accepted = create_sfx(req)
    row = _await_job(accepted.job_id, CONFIG.sfx_timeout_s + 60)
    data, media = _first_output_bytes(row)
    return Response(content=data, media_type=media, headers={"X-Job-Id": row["job_id"]})


@app.post("/v1/music/compose")
def shim_music_compose(body: dict[str, Any], _: None = Depends(require_key)) -> Response:
    """ElevenLabs Eleven Music: {prompt, music_length_ms}."""
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="chybí 'prompt'")
    length_ms = int(body.get("music_length_ms") or 40_000)
    req = MusicRequest(
        prompt=prompt,
        duration_s=length_ms / 1000.0,
        format=_shim_format(body.get("output_format")),
    )
    accepted = create_music(req)
    row = _await_job(accepted.job_id, CONFIG.music_timeout_s + 120)
    data, media = _first_output_bytes(row)
    return Response(content=data, media_type=media, headers={"X-Job-Id": row["job_id"]})


@app.exception_handler(BackendError)
def _backend_error(_: Any, exc: BackendError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})
