"""Registr backendů."""

from __future__ import annotations

from .acestep import AceStepBackend
from .base import Backend, BackendError, BackendUnavailable, GenSpec, RawAudio
from .sfxhttp import SfxHTTPBackend
from .ttshttp import TtsHTTPBackend, TtsSpec

__all__ = [
    "AceStepBackend",
    "Backend",
    "BackendError",
    "BackendUnavailable",
    "GenSpec",
    "RawAudio",
    "SfxHTTPBackend",
    "TtsHTTPBackend",
    "TtsSpec",
]
