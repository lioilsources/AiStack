"""TTS runtime na CPU: Kokoro-82M (ONNX) + Piper (ONNX). Kontejner audio-tts.

Oba enginy jsou malé (Kokoro 82M parametrů, Piper hlas ~60 MB) a na Grace
CPU běží rychleji než v reálném čase, takže kontejner smí běžet pořád — bez
GPU, nezávisle na profilu SPARKu. Kontrakt sdílí s GPU runtime
(tts_gpu_server.py) a s adaptérem app/backends/ttshttp.py:

    GET  /health   GET /models   GET /voices
    POST /models/{name}/load|unload
    POST /synthesize   multipart request=<JSON>  → audio/wav
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import numpy as np
import onnxruntime as ort
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-cpu")

MODELS_DIR = Path(os.environ.get("TTS_MODELS_DIR", "/models"))
KOKORO_DIR = MODELS_DIR / os.environ.get("KOKORO_DIR", "kokoro-82m")
# model.onnx (fp32, 310 MB) zní nejlíp; model_quantized.onnx (int8, 90 MB)
# je ~2× rychlejší, ale s občasným chrčením na sykavkách.
KOKORO_ONNX = os.environ.get("KOKORO_ONNX", "onnx/model.onnx")
PIPER_DIR = MODELS_DIR / os.environ.get("PIPER_DIR", "piper-voices")
# Vlákna onnxruntime na jednu inferenci. Grace má 20 jader a vedle běží
# ComfyUI/vLLM — TTS nemá sebrat všechno.
THREADS = int(os.environ.get("TTS_CPU_THREADS", "4"))
PRELOAD = os.environ.get("TTS_PRELOAD", "kokoro-82m,piper-cs-kasandra-medium")

# Piper hlas → jméno modelu v katalogu orchestrátoru (app/catalog.py).
PIPER_MODELS = {
    "piper-cs-kasandra-medium": "cs_CZ-kasandra-medium",
    "piper-cs-jirka-medium": "cs_CZ-jirka-medium",
    "piper-cs-jirka-low": "cs_CZ-jirka-low",
}


class SynthRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    language: str = "en-us"
    model: str
    voice: str = ""
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: Optional[int] = None
    params: dict[str, Any] = Field(default_factory=dict)


_lock = threading.Lock()
_kokoro: Any = None
_piper: dict[str, Any] = {}


def _session_options() -> ort.SessionOptions:
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = THREADS
    opts.inter_op_num_threads = 1
    return opts


# --- Kokoro ---


def _kokoro_voices_npz() -> Path:
    """onnx-community export veze hlasy jako syrové float32 `voices/<jméno>.bin`;
    kokoro-onnx chce jeden .npz. Složí se při prvním načtení do /tmp."""
    out = Path(os.environ.get("KOKORO_VOICES_NPZ", "/tmp/kokoro-voices.npz"))
    if out.is_file():
        return out
    voices = {}
    for path in sorted((KOKORO_DIR / "voices").glob("*.bin")):
        arr = np.fromfile(path, dtype=np.float32)
        if arr.size % 256:
            log.warning("kokoro: %s nemá násobek 256 floatů, přeskakuji", path.name)
            continue
        voices[path.stem] = arr.reshape(-1, 1, 256)
    if not voices:
        raise HTTPException(status_code=503, detail=f"kokoro: žádné hlasy v {KOKORO_DIR / 'voices'} — make download-audio-tts")
    np.savez(out, **voices)
    return out


def _kokoro_available() -> bool:
    return (KOKORO_DIR / KOKORO_ONNX).is_file() and any((KOKORO_DIR / "voices").glob("*.bin"))


def _load_kokoro() -> Any:
    global _kokoro
    with _lock:
        if _kokoro is not None:
            return _kokoro
        if not _kokoro_available():
            raise HTTPException(status_code=503, detail=f"kokoro: váhy chybí v {KOKORO_DIR} — make download-audio-tts")
        from kokoro_onnx import Kokoro

        started = time.time()
        sess = ort.InferenceSession(
            str(KOKORO_DIR / KOKORO_ONNX), sess_options=_session_options(), providers=["CPUExecutionProvider"],
        )
        _kokoro = Kokoro.from_session(sess, str(_kokoro_voices_npz()))
        log.info("kokoro načten za %.1fs (%s, %d vláken)", time.time() - started, KOKORO_ONNX, THREADS)
        return _kokoro


def _kokoro_synth(req: SynthRequest) -> tuple[np.ndarray, int]:
    kokoro = _load_kokoro()
    if req.voice not in kokoro.voices:
        raise HTTPException(status_code=404, detail=f"kokoro nemá hlas {req.voice!r}")
    audio, sr = kokoro.create(req.text, voice=req.voice, speed=req.speed, lang=req.language)
    return np.asarray(audio, dtype=np.float32), int(sr)


# --- Piper ---


def _piper_path(model: str) -> Path | None:
    voice = PIPER_MODELS.get(model)
    if voice is None:
        return None
    hits = sorted(PIPER_DIR.rglob(f"{voice}.onnx"))
    return hits[0] if hits else None


def _load_piper(model: str) -> Any:
    with _lock:
        if model in _piper:
            return _piper[model]
        path = _piper_path(model)
        if path is None:
            raise HTTPException(status_code=503, detail=f"piper: {model} není stažený v {PIPER_DIR} — make download-audio-tts")
        from piper import PiperVoice

        started = time.time()
        voice = PiperVoice.load(path, config_path=f"{path}.json", use_cuda=False)
        # PiperVoice si session zakládá s výchozími SessionOptions (= všechna
        # fyzická jádra) a OMP_NUM_THREADS pip build onnxruntime ignoruje —
        # proto se session vymění za vlastní s limitem vláken.
        voice.session = ort.InferenceSession(
            str(path), sess_options=_session_options(), providers=["CPUExecutionProvider"],
        )
        _piper[model] = voice
        log.info("piper %s načten za %.1fs", model, time.time() - started)
        return voice


def _piper_synth(req: SynthRequest) -> tuple[np.ndarray, int]:
    from piper import SynthesisConfig

    voice = _load_piper(req.model)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        # length_scale je převrácená rychlost.
        voice.synthesize_wav(req.text, wav, syn_config=SynthesisConfig(length_scale=1.0 / req.speed))
    buf.seek(0)
    audio, sr = sf.read(buf, dtype="float32")
    return audio, int(sr)


# --- API ---


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Natáhnout předem: oba modely dohromady drží pod 1 GB a první replika
    # MemeShorts pak nečeká na načtení.
    for name in [n.strip() for n in PRELOAD.split(",") if n.strip()]:
        try:
            _load_kokoro() if name == "kokoro-82m" else _load_piper(name)
        except Exception as exc:  # noqa: BLE001 — server má nastartovat i bez vah
            log.warning("předběžné načtení %s selhalo: %s", name, getattr(exc, "detail", exc))
    yield


app = FastAPI(title="AiStack audio-tts (CPU)", version="1.0.0", lifespan=lifespan)


def _loaded() -> list[str]:
    return (["kokoro-82m"] if _kokoro is not None else []) + sorted(_piper)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "audio-tts", "engines": ["kokoro", "piper"], "loaded": _loaded(), "device": "cpu"}


@app.get("/models")
def models() -> dict[str, Any]:
    available = (["kokoro-82m"] if _kokoro_available() else []) + [m for m in PIPER_MODELS if _piper_path(m)]
    return {"available": available, "loaded": _loaded()}


@app.get("/voices")
def voices() -> list[dict[str, Any]]:
    out = [{"id": p.stem, "model": "kokoro-82m"} for p in sorted((KOKORO_DIR / "voices").glob("*.bin"))]
    out += [{"id": v, "model": m} for m, v in PIPER_MODELS.items() if _piper_path(m)]
    return out


@app.post("/models/{name}/load")
def load_model(name: str) -> dict[str, str]:
    if name == "kokoro-82m":
        _load_kokoro()
    elif name in PIPER_MODELS:
        _load_piper(name)
    else:
        raise HTTPException(status_code=404, detail=f"neznámý model {name!r}")
    return {"model": name, "status": "loaded"}


@app.post("/models/{name}/unload")
def unload_model(name: str) -> dict[str, str]:
    global _kokoro
    with _lock:
        if name == "kokoro-82m" and _kokoro is not None:
            _kokoro = None
            return {"model": name, "status": "unloaded"}
        if _piper.pop(name, None) is not None:
            return {"model": name, "status": "unloaded"}
    return {"model": name, "status": "nebyl načtený"}


@app.post("/synthesize")
def synthesize(request: str = Form(...), reference: UploadFile | None = File(default=None)) -> Response:
    try:
        req = SynthRequest(**json.loads(request))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"neplatný request: {exc}") from exc
    if reference is not None:
        raise HTTPException(status_code=422, detail="Kokoro ani Piper neumí klonovat hlas z reference")

    started = time.time()
    try:
        if req.model == "kokoro-82m":
            audio, sr = _kokoro_synth(req)
        elif req.model in PIPER_MODELS:
            audio, sr = _piper_synth(req)
        else:
            raise HTTPException(status_code=404, detail=f"neznámý model {req.model!r}")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — chyba modelu musí ven jako 502
        log.exception("syntéza selhala")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    elapsed = time.time() - started

    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    seconds = len(audio) / sr if sr else 0.0
    log.info("%s/%s: %.2fs pro %.2fs řeči (RTF %.2f)", req.model, req.voice or "-", elapsed, seconds,
             elapsed / seconds if seconds else 0.0)
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={
            "X-Model": req.model,
            "X-Voice": req.voice or PIPER_MODELS.get(req.model, ""),
            "X-Gen-Seconds": f"{elapsed:.2f}",
            "X-Sample-Rate": str(sr),
            "X-Watermark": "",
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("TTS_HOST", "0.0.0.0"), port=int(os.environ.get("TTS_PORT", "8003")))
