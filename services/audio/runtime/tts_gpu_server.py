"""TTS runtime na GPU: XTTS-v2 nebo Chatterbox Multilingual (podle TTS_ENGINE).

Jeden soubor pro oba kontejnery (audio-tts-xtts, audio-tts-chatterbox): HTTP
kontrakt, cache referencí, seed a odkládání modelu jsou stejné, liší se jen
načtení a volání modelu. Každý engine má vlastní image, protože jejich piny
se nesnesou (Chatterbox chce transformers 5.2, coqui-tts 4.57).

Kontrakt sdílí s tts_cpu_server.py a s app/backends/ttshttp.py:

    GET  /health   GET /models   GET /voices
    POST /models/{name}/load|unload
    POST /synthesize   multipart request=<JSON>, reference=<WAV>  → audio/wav

Klonování: orchestrátor posílá referenční vzorek s každým requestem; latenty
(XTTS) / conditionals (Chatterbox) se cachují podle sha256 vzorku, takže
druhá a další replika téže postavy je nepočítá znovu.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-gpu")

ENGINE = os.environ.get("TTS_ENGINE", "xtts")
MODELS_DIR = Path(os.environ.get("TTS_MODELS_DIR", "/models"))
DEVICE = os.environ.get("TTS_DEVICE", "cuda")
IDLE_UNLOAD_S = int(os.environ.get("TTS_IDLE_UNLOAD_S", "900"))
PRELOAD = os.environ.get("TTS_PRELOAD", "").lower() in ("1", "true", "yes")
REF_CACHE_SIZE = int(os.environ.get("TTS_REF_CACHE", "32"))

XTTS_DIR = MODELS_DIR / os.environ.get("XTTS_DIR", "xtts-v2")
CHATTERBOX_DIR = MODELS_DIR / os.environ.get("CHATTERBOX_DIR", "chatterbox")
CHATTERBOX_CS_DIR = MODELS_DIR / os.environ.get("CHATTERBOX_CS_DIR", "chatterbox-cs")
# v3 = "Chatterbox Multilingual V3" (README upstreamu: lepší podobnost hlasu,
# méně halucinací); upstream kód má výchozí pořád v2. Přepnout jde env.
CHATTERBOX_T3 = os.environ.get("CHATTERBOX_T3", "t3_mtl23ls_v3.safetensors")
# Český T3 (Thomcles/Chatterbox-TTS-Czech) nemá popsaný jazykový tag. Tag
# "[xx]" se přidává před text jen pro jazyky ze SUPPORTED_LANGUAGES; prázdné
# = bez tagu. NEOVĚŘENO — při prvním smoke testu poslechem porovnat "" a "pl".
CHATTERBOX_CS_LANG_ID = os.environ.get("CHATTERBOX_CS_LANG_ID", "")
# Chatterbox generuje nejvýš 1000 speech tokenů (~40 s); delší text se dělí
# po větách a skládá s krátkou pauzou.
CHUNK_CHARS = int(os.environ.get("TTS_CHUNK_CHARS", "280"))
CHUNK_PAUSE_S = float(os.environ.get("TTS_CHUNK_PAUSE_S", "0.12"))

ENGINE_MODELS = {"xtts": ("xtts-v2",), "chatterbox": ("chatterbox-multilingual", "chatterbox-cs")}

# XTTS chce zh-cn, API posílá ISO 639-1.
XTTS_LANG = {"zh": "zh-cn"}


class SynthRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    language: str = "en"
    model: str
    voice: str = ""
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: Optional[int] = None
    params: dict[str, Any] = Field(default_factory=dict)


_lock = threading.Lock()  # načítání i generace drží GPU — serializovat
_model: Any = None
_model_name = ""
_last_used = 0.0
_default_conds: Any = None  # Chatterbox vestavěný hlas (conds.pt)
_ref_cache: "OrderedDict[str, Any]" = OrderedDict()


def _seed_all(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed % (2**32))
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _split_text(text: str, limit: int) -> list[str]:
    """Po větách, věty delší než limit po čárkách, nakonec natvrdo."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]
    chunks: list[str] = []
    cur = ""
    for sentence in sentences:
        parts = [sentence]
        if len(sentence) > limit:
            parts = [p.strip() for p in re.split(r"(?<=[,;:])\s+", sentence) if p.strip()]
        for part in parts:
            while len(part) > limit:
                cut = part.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                chunks.append(part[:cut].strip())
                part = part[cut:].strip()
            if cur and len(cur) + 1 + len(part) > limit:
                chunks.append(cur)
                cur = part
            else:
                cur = f"{cur} {part}".strip()
    if cur:
        chunks.append(cur)
    return chunks


# --- dostupnost vah ---


def _available() -> list[str]:
    if ENGINE == "xtts":
        return ["xtts-v2"] if (XTTS_DIR / "model.pth").is_file() else []
    out = []
    base_ok = all((CHATTERBOX_DIR / f).is_file() for f in ("ve.pt", "s3gen.pt", CHATTERBOX_T3))
    if base_ok:
        out.append("chatterbox-multilingual")
        if (CHATTERBOX_CS_DIR / "t3_cs.safetensors").is_file():
            out.append("chatterbox-cs")
    return out


# --- XTTS ---


def _patch_xtts_audio_loader() -> None:
    """XTTS čte referenci přes torchaudio.load, který od torchaudio 2.9 chce
    torchcodec (na aarch64 + cu130 bez ověřeného wheelu). Reference je od
    orchestrátoru vždy mono WAV, takže stačí soundfile."""
    import torchaudio
    from TTS.tts.models import xtts as xtts_mod

    def load_audio(audiopath, sampling_rate):
        data, sr = sf.read(str(audiopath), dtype="float32", always_2d=True)
        audio = torch.from_numpy(data.mean(axis=1)).unsqueeze(0)
        if sr != sampling_rate:
            audio = torchaudio.functional.resample(audio, sr, sampling_rate)
        return audio.clamp_(-1, 1)

    xtts_mod.load_audio = load_audio


def _load_xtts() -> Any:
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    _patch_xtts_audio_loader()
    config = XttsConfig()
    config.load_json(str(XTTS_DIR / "config.json"))
    model = Xtts.init_from_config(config)
    model.load_checkpoint(config, checkpoint_dir=str(XTTS_DIR), use_deepspeed=False)
    # Ne `model.to(...).eval()`: Xtts.eval() přetěžuje nn.Module.eval a vrací None.
    model.to(DEVICE)
    model.eval()
    return model


def _xtts_voices(model: Any) -> list[str]:
    manager = getattr(model, "speaker_manager", None)
    return sorted(manager.speakers) if manager is not None else []


def _xtts_synth(model: Any, req: SynthRequest, ref: Path | None, ref_key: str) -> tuple[np.ndarray, int]:
    if ref is not None:
        latents = _ref_cache.get(ref_key)
        if latents is None:
            latents = model.get_conditioning_latents(audio_path=[str(ref)])
            _cache_ref(ref_key, latents)
        gpt_cond_latent, speaker_embedding = latents
    else:
        voice = req.voice or "Ana Florence"
        speakers = model.speaker_manager.speakers if model.speaker_manager else {}
        if voice not in speakers:
            raise HTTPException(status_code=404, detail=f"xtts nemá vestavěný hlas {voice!r}")
        gpt_cond_latent, speaker_embedding = speakers[voice].values()

    p = req.params
    out = model.inference(
        req.text,
        XTTS_LANG.get(req.language, req.language),
        gpt_cond_latent,
        speaker_embedding,
        temperature=float(p.get("temperature", 0.75)),
        top_p=float(p.get("top_p", 0.85)),
        repetition_penalty=float(p.get("repetition_penalty", 10.0)),
        speed=req.speed,
        # XTTS má limit znaků na větu (čeština 186) — dělení si udělá sám.
        enable_text_splitting=True,
    )
    wav = out["wav"]
    wav = wav.detach().cpu().numpy() if isinstance(wav, torch.Tensor) else np.asarray(wav)
    return wav.astype(np.float32).reshape(-1), 24000


# --- Chatterbox ---


def _load_t3(model: Any, path: Path) -> None:
    from safetensors.torch import load_file

    state = load_file(str(path), device="cpu")
    if "model" in state.keys():
        state = state["model"][0]
    # Po první syntéze má T3 navíc podmodul `patched_model` (obal nad tfmr,
    # staví se líně při `compiled = False`). Striktní load_state_dict pak hlásí
    # chybějící `patched_model.*` klíče — 6. 10. 2026 tak padal přechod
    # multilingual → chatterbox-cs, i když checkpointy mají stejných 292 klíčů.
    # Obal se zahodí a při další syntéze postaví znovu nad novými vahami.
    if getattr(model.t3, "patched_model", None) is not None:
        del model.t3.patched_model
    model.t3.compiled = False
    model.t3.load_state_dict(state)
    model.t3.to(DEVICE).eval()


def _load_chatterbox(name: str) -> Any:
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    model = ChatterboxMultilingualTTS.from_local(CHATTERBOX_DIR, DEVICE, t3_model=CHATTERBOX_T3)
    if name == "chatterbox-cs":
        _load_t3(model, CHATTERBOX_CS_DIR / "t3_cs.safetensors")
    return model


def _switch_chatterbox(model: Any, name: str) -> None:
    """Multilingual a český T3 sdílí VE i S3Gen — přepne se jen T3 (~2 GB)."""
    _load_t3(model, CHATTERBOX_CS_DIR / "t3_cs.safetensors" if name == "chatterbox-cs" else CHATTERBOX_DIR / CHATTERBOX_T3)


def _chatterbox_synth(model: Any, req: SynthRequest, ref: Path | None, ref_key: str) -> tuple[np.ndarray, int]:
    p = req.params
    exaggeration = float(p.get("exaggeration", 0.5))
    if ref is not None:
        conds = _ref_cache.get(ref_key)
        if conds is None:
            model.prepare_conditionals(str(ref), exaggeration=exaggeration)
            conds = model.conds
            _cache_ref(ref_key, conds)
        model.conds = conds
    else:
        if req.voice not in ("", "default"):
            raise HTTPException(status_code=404, detail=f"chatterbox má jen vestavěný hlas 'default', ne {req.voice!r}")
        if _default_conds is None:
            raise HTTPException(status_code=422, detail="chatterbox: chybí conds.pt — bez reference není čím mluvit")
        model.conds = _default_conds

    if req.model == "chatterbox-cs":
        language_id = CHATTERBOX_CS_LANG_ID or None
    else:
        language_id = req.language
    if req.speed != 1.0:
        log.info("chatterbox rychlost neumí — speed=%.2f ignoruji (orchestrátor ji nepřepočítává)", req.speed)

    pieces = []
    pause = np.zeros(int(CHUNK_PAUSE_S * model.sr), dtype=np.float32)
    for chunk in _split_text(req.text, CHUNK_CHARS):
        wav = model.generate(
            chunk,
            language_id=language_id,
            exaggeration=exaggeration,
            cfg_weight=float(p.get("cfg_weight", 0.5)),
            temperature=float(p.get("temperature", 0.8)),
            repetition_penalty=float(p.get("repetition_penalty", 1.2)),
            top_p=float(p.get("top_p", 1.0)),
        )
        if pieces:
            pieces.append(pause)
        pieces.append(wav.squeeze(0).detach().cpu().numpy().astype(np.float32))
    return np.concatenate(pieces), int(model.sr)


# --- společné ---


def _cache_ref(key: str, value: Any) -> None:
    _ref_cache[key] = value
    _ref_cache.move_to_end(key)
    while len(_ref_cache) > REF_CACHE_SIZE:
        _ref_cache.popitem(last=False)


def _load(name: str) -> Any:
    global _model, _model_name, _default_conds, _last_used
    if name not in _available():
        raise HTTPException(status_code=503, detail=f"{name}: váhy nejsou stažené v {MODELS_DIR} — make download-audio-tts")
    if _model is not None and _model_name == name:
        return _model
    started = time.time()
    if _model is not None and ENGINE == "chatterbox":
        _switch_chatterbox(_model, name)
    else:
        _model = _load_xtts() if ENGINE == "xtts" else _load_chatterbox(name)
        if ENGINE == "chatterbox":
            _default_conds = _model.conds
    # Latenty závisí na modelu (u Chatterboxu T3) — po přepnutí neplatí.
    _ref_cache.clear()
    _model_name = name
    _last_used = time.time()
    log.info("%s načten za %.1fs (%s)", name, time.time() - started, DEVICE)
    return _model


def _unload() -> bool:
    global _model, _model_name, _default_conds
    if _model is None:
        return False
    _model, _model_name, _default_conds = None, "", None
    _ref_cache.clear()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    log.info("model odložen")
    return True


def _idle_reaper() -> None:
    while True:
        time.sleep(30)
        if _model is not None and time.time() - _last_used > IDLE_UNLOAD_S:
            with _lock:
                if _model is not None and time.time() - _last_used > IDLE_UNLOAD_S:
                    log.info("nečinný %ds — odkládám", IDLE_UNLOAD_S)
                    _unload()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if IDLE_UNLOAD_S > 0:
        threading.Thread(target=_idle_reaper, daemon=True).start()
    if PRELOAD and _available():
        try:
            with _lock:
                _load(_available()[0])
        except Exception:  # noqa: BLE001 — server má nastartovat i bez modelu
            log.exception("předběžné načtení selhalo")
    yield


app = FastAPI(title=f"AiStack audio-tts-{ENGINE}", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": f"audio-tts-{ENGINE}",
        "engines": [ENGINE],
        "loaded": [_model_name] if _model is not None else [],
        "device": DEVICE,
        "cuda": torch.cuda.is_available(),
    }


@app.get("/models")
def models() -> dict[str, Any]:
    return {"available": _available(), "loaded": [_model_name] if _model is not None else []}


@app.get("/voices")
def voices() -> list[dict[str, Any]]:
    if ENGINE == "chatterbox":
        return [{"id": "default", "model": m} for m in _available()]
    # Vestavěné hlasy XTTS jsou v speakers_xtts.pth — bez natažení modelu je
    # znát nejde a natahovat kvůli výpisu GPU model nemá smysl.
    if _model is None:
        return []
    return [{"id": v, "model": "xtts-v2"} for v in _xtts_voices(_model)]


@app.post("/models/{name}/load")
def load_model(name: str) -> dict[str, str]:
    with _lock:
        _load(name)
    return {"model": name, "status": "loaded"}


@app.post("/models/{name}/unload")
def unload_model(name: str) -> dict[str, str]:
    with _lock:
        if _model_name != name:
            return {"model": name, "status": "nebyl načtený"}
        _unload()
    return {"model": name, "status": "unloaded"}


@app.post("/synthesize")
def synthesize(request: str = Form(...), reference: UploadFile | None = File(default=None)) -> Response:
    global _last_used
    try:
        req = SynthRequest(**json.loads(request))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"neplatný request: {exc}") from exc
    if req.model not in ENGINE_MODELS.get(ENGINE, ()):
        raise HTTPException(status_code=404, detail=f"tenhle kontejner ({ENGINE}) neumí model {req.model!r}")

    ref_bytes = reference.file.read() if reference is not None else b""
    seed = req.seed if req.seed is not None else int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF

    with tempfile.TemporaryDirectory(prefix="tts-ref-") as tmpdir, _lock:
        ref_path = None
        ref_key = ""
        if ref_bytes:
            ref_key = f"{req.model}:{hashlib.sha256(ref_bytes).hexdigest()}"
            ref_path = Path(tmpdir) / "ref.wav"
            ref_path.write_bytes(ref_bytes)
        started = time.time()
        try:
            model = _load(req.model)
            _seed_all(seed)
            if ENGINE == "xtts":
                audio, sr = _xtts_synth(model, req, ref_path, ref_key)
            else:
                audio, sr = _chatterbox_synth(model, req, ref_path, ref_key)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — chyba modelu musí ven jako 502
            log.exception("syntéza selhala")
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
        _last_used = time.time()
        elapsed = _last_used - started

    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    seconds = len(audio) / sr if sr else 0.0
    log.info("%s: %.1fs pro %.1fs řeči (%s)", req.model, elapsed, seconds, "klon" if ref_bytes else req.voice or "default")
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={
            "X-Model": req.model,
            "X-Voice": "reference" if ref_bytes else (req.voice or "default"),
            "X-Seed": str(seed),
            "X-Gen-Seconds": f"{elapsed:.2f}",
            "X-Sample-Rate": str(sr),
            "X-Watermark": "resemble-perth" if ENGINE == "chatterbox" else "",
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("TTS_HOST", "0.0.0.0"), port=int(os.environ.get("TTS_PORT", "8004")))
