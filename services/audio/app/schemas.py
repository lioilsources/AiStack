"""Request/response modely veřejného API (plán §3.2)."""

from __future__ import annotations

from typing import Literal, Optional

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


class Output(BaseModel):
    url: str
    filename: str
    duration: float
    loudness_lufs: Optional[float] = None
    true_peak_db: Optional[float] = None
    seed: Optional[int] = None
    sha256: str = ""
    bytes: int = 0


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
