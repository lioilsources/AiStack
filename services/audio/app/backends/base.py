"""Rozhraní backendu. Job worker nesmí vědět, jestli generuje ACE-Step nebo MOSS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class GenSpec:
    """Co se má vygenerovat — společný jmenovatel hudby i SFX."""

    prompt: str
    duration_s: float
    seed: int | None = None
    lyrics: str = ""
    instrumental: bool = True
    bpm: int | None = None
    key: str = ""
    model: str = ""


@dataclass(frozen=True)
class RawAudio:
    path: Path
    seed: int | None
    model: str


class BackendError(RuntimeError):
    pass


class Backend(Protocol):
    name: str
    kind: str  # "music" | "sfx"

    def generate(self, spec: GenSpec, workdir: Path) -> RawAudio:
        """Vygeneruje jednu stopu a vrátí cestu k surovému WAV ve workdir."""

    def health(self) -> tuple[bool, str]:
        """(dostupný, detail) — používá /v1/audio/models a healthcheck."""

    def loaded_models(self) -> list[str]:
        """Modely, které má runtime právě natažené."""
