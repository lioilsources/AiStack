"""Request/response modely veřejného API (plán §3.2)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

AudioFormat = Literal["ogg", "wav", "mp3", "flac"]


class MusicRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    duration_s: float = Field(default=40.0, gt=0, le=600)
    seed: Optional[int] = None
    lyrics: str = ""
    instrumental: bool = True
    bpm: Optional[int] = Field(default=None, ge=20, le=300)
    key: str = ""
    loop: bool = True
    format: AudioFormat = "ogg"
    variations: int = Field(default=1, ge=1, le=8)

    # Volitelný override modelu; prázdné = default backendu ze konfigurace.
    model: str = ""


class SfxRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    duration_s: float = Field(default=2.0, gt=0, le=47)
    seed: Optional[int] = None
    variations: int = Field(default=1, ge=1, le=8)
    mono: bool = True
    format: AudioFormat = "ogg"
    model: str = ""


class AltFile(BaseModel):
    """Tatáž varianta v jiném formátu (vibe: WAV vedle MP3)."""

    url: str
    filename: str
    sha256: str = ""
    bytes: int = 0


class Output(BaseModel):
    url: str
    filename: str
    duration: float
    loudness_lufs: Optional[float] = None
    true_peak_db: Optional[float] = None
    seed: Optional[int] = None
    sha256: str = ""
    bytes: int = 0
    alt: dict[str, AltFile] = Field(default_factory=dict)


class JobStatus(BaseModel):
    job_id: str
    kind: Literal["music", "sfx"]
    status: Literal["queued", "running", "done", "error"]
    model: str = ""
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    queue_position: Optional[int] = None
    error: str = ""
    outputs: list[Output] = Field(default_factory=list)
    # "generate" | "vibe" | "analyze"
    task: str = "generate"
    # Analýza předlohy (analyze) nebo manifest (vibe); u ostatních null.
    result: Optional[dict[str, Any]] = None


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    queue_position: int


class ModelInfo(BaseModel):
    name: str
    kind: Literal["music", "sfx"]
    backend: str
    license: str
    license_url: str
    commercial: bool
    note: str = ""
    upstream: str
    available: bool = False
    loaded: bool = False
    detail: str = ""


# --- vibe z předlohy ---


class SampleOut(BaseModel):
    sample_id: str
    filename: str
    bytes: int
    source_duration_s: float
    window_start_s: float
    duration_s: float
    ref_start_s: Optional[float] = None
    # Poslední analýza, pokud už proběhla — aplikace po reuploadu nemusí
    # analyzovat znovu.
    analysis: Optional[dict[str, Any]] = None


class VibeAnalyzeRequest(BaseModel):
    sample_id: str = Field(min_length=8, max_length=64)


class VibeRequest(BaseModel):
    """Plán vibe §2 `VibeRequest`, ale nad nahranou předlohou (sample_id)."""

    sample_id: str = Field(min_length=8, max_length=64)
    mode: Literal["vibe", "groove"] = "vibe"
    # Prázdné = caption z analýzy (plánovaný `caption_override`).
    caption: str = Field(default="", max_length=4000)
    user_hint: str = Field(default="", max_length=500)
    bpm: Optional[int] = Field(default=None, ge=30, le=300)
    keyscale: str = Field(default="", max_length=16)
    timesignature: str = Field(default="", max_length=4)
    # None → délka předlohy (vibe smí být delší, groove má vždy délku předlohy).
    duration_s: Optional[float] = Field(default=None, ge=10, le=240)
    # 0.5: měřeno, pod tím cover rytmus předlohy nedrží (NOTES.md).
    cover_strength: float = Field(default=0.5, ge=0.0, le=1.0)
    # Vibe: nechat 5Hz LM rozvrhnout strukturu. Pomalejší a nejde zopakovat.
    lm_plan: bool = False
    variations: int = Field(default=2, ge=1, le=4)
    seed: Optional[int] = Field(default=None, ge=0, le=2**31 - 1)
    # None → z analýzy.
    instrumental: Optional[bool] = None
    lyrics: str = Field(default="", max_length=4000)
    vocal_language: str = Field(default="", max_length=16)
    format: AudioFormat = "mp3"
