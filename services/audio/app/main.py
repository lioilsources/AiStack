"""services/audio — provider-agnostické API pro generování hudby a SFX.

Kirian ani jiný klient nesmí vědět, že pod tím jede ACE-Step nebo MOSS. Proto
tady žije jen fronta, post-processing a katalog licencí; modely běží ve svých
kontejnerech (audio-music, audio-sfx) a mluví se s nimi přes adaptéry.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse

from .backends import AceStepBackend, Backend, BackendError, SfxHTTPBackend
from .catalog import CATALOG, EXCLUDED, by_kind
from .config import Config
from .jobs import Job, Runner, make_spec_from_request
from .postproc import set_ogg_quality
from .schemas import JobAccepted, JobStatus, ModelInfo, MusicRequest, Output, SfxRequest
from .store import Store

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
backends: dict[str, Backend] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global CONFIG, store, runner
    CONFIG = Config()
    set_ogg_quality(CONFIG.ogg_quality)
    Path(CONFIG.data_dir).mkdir(parents=True, exist_ok=True)

    store = Store(CONFIG.db_path)
    backends.clear()
    if CONFIG.music_url:
        backends["music"] = AceStepBackend(
            CONFIG.music_url, CONFIG.music_model, CONFIG.music_timeout_s
        )
    if CONFIG.sfx_url:
        backends["sfx"] = SfxHTTPBackend(
            CONFIG.sfx_url, CONFIG.sfx_model, CONFIG.sfx_timeout_s
        )

    orphans = store.requeue_orphans()
    if orphans:
        log.warning("%d jobů zachyceno restartem, označeno jako chybové", orphans)

    runner = Runner(CONFIG, store, backends)
    runner.start()
    log.info("audio ready — music=%s sfx=%s", CONFIG.music_url or "-", CONFIG.sfx_url or "-")

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
        "idle_s": {kind: _round_or_none(runner.idle_seconds(kind)) for kind in ("music", "sfx")}
        if runner is not None
        else {},
    }


@app.get("/v1/audio/models", response_model=list[ModelInfo])
def list_models(_: None = Depends(require_key)) -> list[ModelInfo]:
    infos: list[ModelInfo] = []
    for kind in ("music", "sfx"):
        backend = backends.get(kind)
        backend_ok, detail = backend.health() if backend else (False, "backend není nakonfigurován")
        loaded = set(backend.loaded_models()) if backend and backend_ok else set()
        for spec in by_kind(kind):
            served_detail = detail
            if not spec.served:
                served_detail = "žádný runtime tenhle model neobsluhuje — viz poznámka"
            infos.append(
                ModelInfo(
                    name=spec.name,
                    kind=spec.kind,
                    backend=spec.backend,
                    license=spec.license,
                    license_url=spec.license_url,
                    commercial=spec.commercial,
                    note=spec.note,
                    upstream=spec.repo,
                    available=backend_ok and spec.served,
                    loaded=spec.name in loaded,
                    detail=served_detail,
                )
            )
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
    if not any(o.get("filename") == filename for o in row["outputs"]):
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
    )


def _media_type(filename: str) -> str:
    return {
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


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
