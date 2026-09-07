"""Minimální HTTP wrapper nad SFX modely (běží v kontejneru audio-sfx).

MOSS-SoundEffect ani Stable Audio Open nevezou vlastní server, takže tenhle
soubor je celý runtime kontrakt: POST /generate vrátí WAV. Modely se natahují
líně a dají se odložit přes /models/{name}/unload, aby vedle vLLM nedržely
paměť, když se negeneruje.

Kontrakt musí sedět s app/backends/sfxhttp.py.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sfx")

MODELS_DIR = os.environ.get("SFX_MODELS_DIR", "/models")
DEFAULT_MODEL = os.environ.get("SFX_DEFAULT_MODEL", "moss-soundeffect-v2")
DEVICE = os.environ.get("SFX_DEVICE", "cuda")
IDLE_UNLOAD_S = int(os.environ.get("SFX_IDLE_UNLOAD_S", "0"))
# MOSS doporučuje 100 kroků. Naměřeno na GB10 se zapnutým torch.compile:
# 100 kroků = 22,5 s, 50 = 11,2 s, 30 = 6,7 s na jeden efekt, čas roste
# lineárně s počtem kroků a na délce klipu do 2,5 s prakticky nezávisí.
# Výchozí zůstává doporučená hodnota — assety jdou do hry, ne do náhledu.
DEFAULT_STEPS = int(os.environ.get("SFX_DEFAULT_STEPS", "100"))
DEFAULT_CFG = float(os.environ.get("SFX_DEFAULT_CFG", "4.0"))

# Adresáře pod MODELS_DIR — musí sedět na jména z scripts/download.sh.
MODEL_DIRS = {
    "moss-soundeffect-v2": "moss-soundeffect-v2",
    "stable-audio-open-1.0": "stable-audio-open-1.0",
    "stable-audio-open-small": "stable-audio-open-small",
}


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    duration_s: float = Field(default=2.0, gt=0, le=47)
    seed: Optional[int] = None
    model: str = ""
    steps: Optional[int] = None
    cfg_scale: Optional[float] = None


class _Loaded:
    def __init__(self, name: str, pipe: Any, sample_rate: int) -> None:
        self.name = name
        self.pipe = pipe
        self.sample_rate = sample_rate
        self.last_used = time.time()


_loaded: dict[str, _Loaded] = {}
_lock = threading.Lock()  # generace i načítání drží GPU — serializovat

@asynccontextmanager
async def lifespan(_: FastAPI):
    if IDLE_UNLOAD_S > 0:
        threading.Thread(target=_idle_reaper, daemon=True).start()
    if os.environ.get("SFX_PRELOAD", "").lower() in ("1", "true", "yes"):
        try:
            _load(DEFAULT_MODEL)
        except Exception:  # noqa: BLE001 — server má nastartovat i bez modelu
            log.exception("předběžné načtení %s selhalo", DEFAULT_MODEL)
    yield


app = FastAPI(title="AiStack audio-sfx runtime", version="1.0.0", lifespan=lifespan)


def _model_path(name: str) -> str:
    subdir = MODEL_DIRS.get(name)
    if subdir is None:
        raise HTTPException(status_code=404, detail=f"neznámý model {name!r}")
    path = os.path.join(MODELS_DIR, subdir)
    # Existence adresáře nestačí: gated repo (Stability) po neúspěšném
    # stažení nechá adresář jen s metadaty, a ten by se tvářil jako hotový
    # model až do chvíle, kdy na něm spadne generace.
    if not _has_weights(path):
        raise HTTPException(
            status_code=503,
            detail=f"model {name!r} není stažený ({path}) — spusť scripts/download.sh sfx",
        )
    return path


def _has_weights(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    for root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith((".safetensors", ".ckpt", ".pth", ".bin")):
                return True
    return False


def _load(name: str) -> _Loaded:
    with _lock:
        if name in _loaded:
            return _loaded[name]
        path = _model_path(name)
        log.info("načítám %s z %s", name, path)
        started = time.time()
        if name.startswith("moss"):
            loaded = _load_moss(name, path)
        else:
            loaded = _load_stable_audio(name, path)
        _loaded[name] = loaded
        log.info("%s načten za %.1fs", name, time.time() - started)
        return loaded


def _load_moss(name: str, path: str) -> _Loaded:
    from moss_soundeffect_v2 import MossSoundEffectPipeline

    pipe = MossSoundEffectPipeline.from_pretrained(
        path, torch_dtype=torch.bfloat16, device=DEVICE
    )
    return _Loaded(name, pipe, sample_rate=48000)


def _load_stable_audio(name: str, path: str) -> _Loaded:
    from diffusers import StableAudioPipeline

    pipe = StableAudioPipeline.from_pretrained(path, torch_dtype=torch.float16)
    pipe = pipe.to(DEVICE)
    return _Loaded(name, pipe, sample_rate=44100)


def _unload(name: str) -> bool:
    with _lock:
        loaded = _loaded.pop(name, None)
    if loaded is None:
        return False
    del loaded
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    log.info("%s odložen", name)
    return True


def _generate_moss(loaded: _Loaded, req: GenerateRequest, seed: int) -> "torch.Tensor":
    torch.manual_seed(seed)
    return loaded.pipe(
        prompt=req.prompt,
        seconds=max(1.0, req.duration_s),
        num_inference_steps=req.steps or DEFAULT_STEPS,
        cfg_scale=req.cfg_scale or DEFAULT_CFG,
    )


def _generate_stable_audio(loaded: _Loaded, req: GenerateRequest, seed: int) -> "torch.Tensor":
    generator = torch.Generator(DEVICE).manual_seed(seed)
    result = loaded.pipe(
        req.prompt,
        negative_prompt="low quality, noisy",
        num_inference_steps=req.steps or DEFAULT_STEPS,
        audio_end_in_s=req.duration_s,
        num_waveforms_per_prompt=1,
        guidance_scale=req.cfg_scale or 7.0,
        generator=generator,
    )
    return result.audios[0]


def _to_wav(audio: Any, sample_rate: int) -> bytes:
    """Znormalizuje výstup na (channels, samples) float32 a zapíše WAV."""
    import numpy as np
    import soundfile as sf

    if isinstance(audio, torch.Tensor):
        array = audio.detach().to(torch.float32).cpu().numpy()
    else:
        array = np.asarray(audio, dtype="float32")

    while array.ndim > 2:  # (B, C, T) → (C, T)
        array = array[0]
    if array.ndim == 1:
        array = array[None, :]
    # soundfile chce (samples, channels)
    data = array.T

    peak = float(abs(data).max()) if data.size else 0.0
    if peak > 1.0:
        data = data / peak

    buf = io.BytesIO()
    sf.write(buf, data, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "audio-sfx",
        "cuda": torch.cuda.is_available(),
        "loaded": list(_loaded),
    }


@app.get("/models")
def models() -> dict[str, Any]:
    available = [n for n, d in MODEL_DIRS.items() if _has_weights(os.path.join(MODELS_DIR, d))]
    return {"available": available, "loaded": list(_loaded), "default": DEFAULT_MODEL}


@app.post("/models/{name}/load")
def load_model(name: str) -> dict[str, str]:
    _load(name)
    return {"model": name, "status": "loaded"}


@app.post("/models/{name}/unload")
def unload_model(name: str) -> dict[str, str]:
    return {"model": name, "status": "unloaded" if _unload(name) else "nebyl načtený"}


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    name = req.model or DEFAULT_MODEL
    loaded = _load(name)
    seed = req.seed if req.seed is not None else int.from_bytes(os.urandom(4), "big")

    with _lock:
        started = time.time()
        try:
            if name.startswith("moss"):
                audio = _generate_moss(loaded, req, seed)
            else:
                audio = _generate_stable_audio(loaded, req, seed)
        except Exception as exc:  # noqa: BLE001 — chyba modelu musí ven jako 502
            log.exception("generace selhala")
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
        loaded.last_used = time.time()
        elapsed = loaded.last_used - started

    wav = _to_wav(audio, loaded.sample_rate)
    log.info("%s: %.1fs pro %.1fs audia (%d B)", name, elapsed, req.duration_s, len(wav))
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"X-Seed": str(seed), "X-Model": name, "X-Gen-Seconds": f"{elapsed:.2f}"},
    )


def _idle_reaper() -> None:
    while True:
        time.sleep(30)
        cutoff = time.time() - IDLE_UNLOAD_S
        for name, loaded in list(_loaded.items()):
            if loaded.last_used < cutoff:
                log.info("%s nečinný %ds — odkládám", name, IDLE_UNLOAD_S)
                _unload(name)


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("SFX_HOST", "0.0.0.0"), port=int(os.environ.get("SFX_PORT", "8002")))
