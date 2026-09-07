"""Adaptér na kontejner audio-sfx (MOSS-SoundEffect / Stable Audio Open).

Ani jeden z těch modelů neveze vlastní server, takže runtime image nese
minimální FastAPI wrapper (`services/audio/runtime/sfx_server.py`) s pevným
kontraktem: POST /generate vrátí rovnou WAV. Generace SFX trvá jednotky
sekund, takže fronta na téhle straně by byla režie navíc.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .base import Backend, BackendError, GenSpec, RawAudio

log = logging.getLogger(__name__)


class SfxHTTPBackend(Backend):
    kind = "sfx"

    def __init__(self, base_url: str, default_model: str, timeout_s: float) -> None:
        self.name = "sfx"
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout_s = timeout_s
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def health(self) -> tuple[bool, str]:
        try:
            resp = self._client.get("/health", timeout=5.0)
        except httpx.HTTPError as exc:
            return False, f"nedostupný: {exc}"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        return True, resp.text[:200]

    def loaded_models(self) -> list[str]:
        try:
            resp = self._client.get("/models", timeout=5.0)
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        payload = resp.json()
        loaded = payload.get("loaded") if isinstance(payload, dict) else None
        return [str(m) for m in loaded] if isinstance(loaded, list) else []

    def load(self, model: str) -> None:
        self._post_model_action(model, "load")

    def unload(self, model: str) -> None:
        self._post_model_action(model, "unload")

    def _post_model_action(self, model: str, action: str) -> None:
        try:
            resp = self._client.post(f"/models/{model}/{action}", timeout=600.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError(f"{action} modelu {model} selhal: {exc}") from exc

    def generate(self, spec: GenSpec, workdir: Path) -> RawAudio:
        model = spec.model or self.default_model
        payload = {
            "prompt": spec.prompt,
            "duration_s": round(spec.duration_s, 3),
            "model": model,
        }
        if spec.seed is not None:
            payload["seed"] = spec.seed

        try:
            resp = self._client.post("/generate", json=payload, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise BackendError(f"audio-sfx nedostupný: {exc}") from exc
        if resp.status_code != 200:
            raise BackendError(f"audio-sfx → HTTP {resp.status_code}: {resp.text[:300]}")

        dst = workdir / "sfx.wav"
        dst.write_bytes(resp.content)
        if dst.stat().st_size == 0:
            raise BackendError("audio-sfx vrátil prázdné audio")

        seed = spec.seed
        header_seed = resp.headers.get("X-Seed")
        if header_seed:
            try:
                seed = int(header_seed)
            except ValueError:
                pass
        return RawAudio(path=dst, seed=seed, model=resp.headers.get("X-Model", model))
