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
    kind: Literal["music", "sfx", "tts"]
    status: Literal["queued", "running", "done", "error"]
    model: str = ""
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    queue_position: Optional[int] = None
    error: str = ""
    outputs: list[Output] = Field(default_factory=list)
    # "generate" | "vibe" | "analyze" | "tts"
    task: str = "generate"
    # Analýza předlohy (analyze) nebo manifest (vibe, tts); u ostatních null.
    result: Optional[dict[str, Any]] = None


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    queue_position: int


class ModelInfo(BaseModel):
    name: str
    kind: Literal["music", "sfx", "tts"]
    backend: str
    license: str
    license_url: str
    commercial: bool
    note: str = ""
    upstream: str
    available: bool = False
    loaded: bool = False
    detail: str = ""
    # Jen TTS (u hudby a SFX prázdné / výchozí).
    license_status: str = "ok"
    attribution: str = ""
    languages: list[str] = Field(default_factory=list)
    cloning: bool = False
    watermark: bool = False


# --- TTS ---

TtsEngine = Literal["", "kokoro", "piper", "xtts", "chatterbox"]


class TtsParams(BaseModel):
    """Ladění GPU enginů; CPU enginy je ignorují."""

    # Chatterbox: expresivita (0.5 neutrální, výš = přehrávanější).
    exaggeration: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    # Chatterbox: síla CFG (nižší = pomalejší, klidnější projev).
    cfg_weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # XTTS i Chatterbox.
    temperature: Optional[float] = Field(default=None, ge=0.05, le=2.0)
    top_p: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    repetition_penalty: Optional[float] = Field(default=None, ge=1.0, le=20.0)


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    # ISO 639-1 (en, cs, …); en-US / cs_CZ se zkrátí.
    language: str = Field(default="en", min_length=2, max_length=16)
    # Preset (af_heart, cs_CZ-kasandra-medium), vestavěný hlas s prefixem
    # (xtts:Ana Florence, chatterbox:default) nebo id uloženého hlasu.
    voice: str = Field(default="", max_length=96)
    engine: TtsEngine = ""
    # Konkrétní model z katalogu (např. chatterbox-cs); prázdné = podle enginu/jazyka.
    model: str = ""
    # Výchozí true: nekomerční nebo licenčně nejasné modely (XTTS-v2, Piper
    # jirka, chatterbox-cs) se bez výslovného false nepoužijí.
    commercial_only: bool = True
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: Optional[int] = Field(default=None, ge=0, le=2**31 - 1)
    variations: int = Field(default=1, ge=1, le=4)
    format: AudioFormat = "wav"
    params: TtsParams = Field(default_factory=TtsParams)


class VoiceInfo(BaseModel):
    id: str
    engine: str
    model: str
    # "preset" (Kokoro, Piper) | "builtin" (vestavěný hlas GPU enginu) | "custom" (uložený vzorek)
    type: str
    language: str = ""
    languages: list[str] = Field(default_factory=list)
    gender: str = ""
    grade: str = ""
    license: str
    commercial: bool
    license_status: str = "ok"
    attribution: str = ""
    watermark: bool = False
    note: str = ""


class StoredVoiceOut(BaseModel):
    voice_id: str
    name: str
    language: str
    gender: str
    rights: str
    source: str
    note: str
    duration_s: float
    sha256: str
    created_at: float
    sample_url: str


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
