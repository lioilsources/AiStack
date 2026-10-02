"""Adaptér na TTS runtime kontejnery.

Tři kontejnery se stejným kontraktem (`services/audio/runtime/tts_*_server.py`):

    audio-tts              Kokoro + Piper (CPU, onnxruntime)
    audio-tts-xtts         XTTS-v2 (GPU)
    audio-tts-chatterbox   Chatterbox Multilingual (+ český T3) (GPU)

    GET  /health   GET /models   GET /voices
    POST /models/{name}/load|unload
    POST /synthesize   multipart: request=<JSON>, reference=<WAV, volitelně>
                       → audio/wav, hlavičky X-Model, X-Voice, X-Seed, X-Watermark

Reference se posílá s každým requestem (desítky až stovky kB): modelový
kontejner nevidí na disk orchestrátoru a cache latentů podle sha256 si drží
sám, takže opakovaná replika téže postavy latenty nepočítá znovu.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .base import BackendError, BackendUnavailable, RawAudio

log = logging.getLogger(__name__)


def tts_down_message(engine: str) -> str:
    where = "audio-tts" if engine in ("kokoro", "piper") else f"audio-tts-{engine}"
    return (
        f"TTS engine {engine} teď neběží (kontejner {where} je dole — GPU enginy jedou jen "
        "v profilech SPARKu, kde na ně zbývá paměť). Zkus to později nebo jiný engine."
    )


@dataclass(frozen=True)
class TtsSpec:
    """Co se má namluvit. `seed` a `duration_s` kvůli společnému runneru."""

    text: str
    language: str
    engine: str
    model: str
    voice: str
    voice_kind: str = "preset"
    engine_language: str = ""
    ref_path: Path | None = None
    speed: float = 1.0
    seed: int | None = None
    # Parametry GPU enginů (exaggeration, cfg_weight, temperature, …).
    params: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0


class TtsHTTPBackend:
    kind = "tts"
    name = "tts"

    def __init__(self, urls: dict[str, str], timeout_s: float) -> None:
        # engine → base URL; prázdná URL = engine není nakonfigurovaný.
        self.urls = {e: u.rstrip("/") for e, u in urls.items() if u}
        self.timeout_s = timeout_s
        self._clients: dict[str, httpx.Client] = {}

    def _client(self, engine: str) -> httpx.Client:
        url = self.urls.get(engine)
        if not url:
            raise BackendUnavailable(f"engine {engine} nemá URL", tts_down_message(engine))
        client = self._clients.get(url)
        if client is None:
            client = httpx.Client(base_url=url, timeout=self.timeout_s)
            self._clients[url] = client
        return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def engines(self) -> list[str]:
        return sorted(self.urls)

    # --- stav ---

    def engine_health(self, engine: str) -> tuple[bool, str]:
        try:
            resp = self._client(engine).get("/health", timeout=5.0)
        except BackendUnavailable as exc:
            return False, exc.detail
        except httpx.HTTPError as exc:
            return False, f"nedostupný: {exc}"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        return True, resp.text[:200]

    def health(self) -> tuple[bool, str]:
        states = {e: self.engine_health(e)[0] for e in self.urls}
        return any(states.values()), json.dumps(states)

    def _get_json(self, engine: str, path: str) -> Any:
        try:
            resp = self._client(engine).get(path, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, BackendUnavailable, ValueError):
            return None

    def engine_models(self, engine: str) -> dict[str, list[str]]:
        payload = self._get_json(engine, "/models")
        if not isinstance(payload, dict):
            return {"available": [], "loaded": []}
        return {
            "available": [str(m) for m in payload.get("available") or []],
            "loaded": [str(m) for m in payload.get("loaded") or []],
        }

    def loaded_models(self) -> list[str]:
        seen: list[str] = []
        for url_engine in {u: e for e, u in self.urls.items()}.values():
            seen += self.engine_models(url_engine)["loaded"]
        return sorted(set(seen))

    def engine_voices(self, engine: str) -> list[dict[str, Any]]:
        payload = self._get_json(engine, "/voices")
        return [v for v in payload if isinstance(v, dict)] if isinstance(payload, list) else []

    # --- řízení ---

    def load(self, model: str, engine: str) -> None:
        self._model_action(model, engine, "load")

    def unload(self, model: str, engine: str) -> None:
        self._model_action(model, engine, "unload")

    def _model_action(self, model: str, engine: str, action: str) -> None:
        try:
            resp = self._client(engine).post(f"/models/{model}/{action}", timeout=600.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError(f"{action} modelu {model} selhal: {exc}") from exc

    # --- generování ---

    def generate(self, spec: TtsSpec, workdir: Path) -> RawAudio:
        client = self._client(spec.engine)
        request = {
            "text": spec.text,
            "language": spec.engine_language or spec.language,
            "model": spec.model,
            "voice": spec.voice if spec.voice_kind != "custom" else "",
            "speed": spec.speed,
            "seed": spec.seed,
            "params": spec.params,
        }
        files = None
        if spec.voice_kind == "custom":
            if spec.ref_path is None or not spec.ref_path.is_file():
                raise BackendError(f"uložený hlas {spec.voice!r} nemá referenční vzorek na disku")
            files = {"reference": (f"{spec.voice}.wav", spec.ref_path.read_bytes(), "audio/wav")}
        try:
            resp = client.post(
                "/synthesize", data={"request": json.dumps(request, ensure_ascii=False)}, files=files,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise BackendUnavailable(str(exc), tts_down_message(spec.engine)) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"{spec.engine} nedostupný: {exc}") from exc
        if resp.status_code != 200:
            raise BackendError(f"{spec.engine} → HTTP {resp.status_code}: {resp.text[:300]}")

        dst = workdir / "tts.wav"
        dst.write_bytes(resp.content)
        if dst.stat().st_size <= 44:
            raise BackendError(f"{spec.engine} vrátil prázdné audio")

        seed = spec.seed
        header_seed = resp.headers.get("X-Seed")
        if header_seed:
            try:
                seed = int(header_seed)
            except ValueError:
                pass
        info = {
            "engine": spec.engine,
            "voice": resp.headers.get("X-Voice", spec.voice),
            "watermark": resp.headers.get("X-Watermark", ""),
            "gen_seconds": resp.headers.get("X-Gen-Seconds", ""),
            "sample_rate": resp.headers.get("X-Sample-Rate", ""),
        }
        return RawAudio(path=dst, seed=seed, model=resp.headers.get("X-Model", spec.model), info=info)
