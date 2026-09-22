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

    # Jen hudba z předlohy (vibe/groove), SFX backend je ignoruje.
    # text2music = nová skladba; cover = přebarvení `src_audio`.
    task_type: str = "text2music"
    # Barva a nálada předlohy; melodii nekopíruje (text2music).
    reference_audio: Path | None = None
    # Předloha, jejíž rytmus a forma se drží (cover).
    src_audio: Path | None = None
    # Jak těsně cover drží předlohu: 1.0 = skoro kopie, 0.2 = volná.
    cover_strength: float = 1.0
    time_signature: str = ""
    vocal_language: str = ""
    # False = caption jde do modelu doslova. U vibe ji uživatel viděl a
    # upravil; LM by ji jinak přepsal po svém.
    rewrite_caption: bool = True
    # 5Hz LM naplánuje strukturu skladby (audio kódy) před difuzí. Pomalejší
    # a nejde zopakovat — nano-vllm seed nerespektuje (services/audio/NOTES.md).
    thinking: bool = True


@dataclass(frozen=True)
class RawAudio:
    path: Path
    seed: int | None
    model: str
    # Co runtime o generaci řekl (modely, metadata) — jde do manifestu.
    info: dict | None = None


class BackendError(RuntimeError):
    pass


# Hláška pro člověka, ne stack: kontejner s modelem na SPARKu přes noc
# vypíná plánovač režimů (WorldLibraryProject rag-schedule.sh, 00:00–07:00),
# takže „nedostupný" je nejčastěji plánovaný stav, ne porucha.
MODEL_DOWN = "Hudební model teď neběží (SPARK je mimo denní režim 07–24, nebo je audio-music dole). Zkus to později."


class BackendUnavailable(BackendError):
    """Model se nedá vůbec kontaktovat — ne že by odpověděl chybou."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(MODEL_DOWN)
        self.detail = detail


class Backend(Protocol):
    name: str
    kind: str  # "music" | "sfx"

    def generate(self, spec: GenSpec, workdir: Path) -> RawAudio:
        """Vygeneruje jednu stopu a vrátí cestu k surovému WAV ve workdir."""

    def health(self) -> tuple[bool, str]:
        """(dostupný, detail) — používá /v1/audio/models a healthcheck."""

    def loaded_models(self) -> list[str]:
        """Modely, které má runtime právě natažené."""

    # Volitelně `analyze(path) -> dict` — poslech předlohy. Umí ho jen hudební
    # backend; bez něj vibe analýza stojí jen na librose.
