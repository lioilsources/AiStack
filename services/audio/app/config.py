"""Konfigurace služby — vše přes env, žádný soubor."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str) -> str:
    return os.environ.get(key) or default


def _env_int(key: str, default: int) -> int:
    return int(_env(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(_env(key, str(default)))


@dataclass(frozen=True)
class Config:
    listen_addr: str = field(default_factory=lambda: _env("LISTEN_ADDR", "0.0.0.0:8093"))
    data_dir: str = field(default_factory=lambda: _env("AUDIO_DATA_DIR", "/data/audio"))
    db_path: str = field(default_factory=lambda: _env("AUDIO_DB_PATH", "/data/audio/jobs.db"))

    # Modelové kontejnery. Prázdná URL = backend není nakonfigurován a
    # /v1/audio/models ho ohlásí jako unavailable místo aby padal až v jobu.
    music_url: str = field(default_factory=lambda: _env("AUDIO_MUSIC_URL", "http://audio-music:8001"))
    sfx_url: str = field(default_factory=lambda: _env("AUDIO_SFX_URL", "http://audio-sfx:8002"))

    # Výchozí modely — jména musí sedět na klíče v catalog.CATALOG.
    music_model: str = field(default_factory=lambda: _env("AUDIO_MUSIC_MODEL", "acestep-v15-turbo"))
    sfx_model: str = field(default_factory=lambda: _env("AUDIO_SFX_MODEL", "moss-soundeffect-v2"))

    # Workerů na kategorii. Modely drží celou GPU, takže 1 je správně —
    # paralelní joby by se jen praly o paměť a každý by běžel pomaleji.
    music_workers: int = field(default_factory=lambda: _env_int("AUDIO_MUSIC_WORKERS", 1))
    sfx_workers: int = field(default_factory=lambda: _env_int("AUDIO_SFX_WORKERS", 1))

    music_timeout_s: float = field(default_factory=lambda: _env_float("AUDIO_MUSIC_TIMEOUT_S", 900.0))
    sfx_timeout_s: float = field(default_factory=lambda: _env_float("AUDIO_SFX_TIMEOUT_S", 300.0))

    # Loudness cíle (plán §3.2). Hudba tišeji než SFX kvůli hlavičce pro mix.
    music_lufs: float = field(default_factory=lambda: _env_float("AUDIO_MUSIC_LUFS", -16.0))
    sfx_lufs: float = field(default_factory=lambda: _env_float("AUDIO_SFX_LUFS", -18.0))
    true_peak_db: float = field(default_factory=lambda: _env_float("AUDIO_TRUE_PEAK_DB", -1.0))
    # Vibe skladby se poslouchají samostatně (telefon, streaming), ne v mixu
    # hry — proto hlasitěji, na obvyklých −14 LUFS (plán vibe §3 krok 4).
    vibe_lufs: float = field(default_factory=lambda: _env_float("AUDIO_VIBE_LUFS", -14.0))

    ogg_quality: int = field(default_factory=lambda: _env_int("AUDIO_OGG_QUALITY", 6))
    loop_crossfade_s: float = field(default_factory=lambda: _env_float("AUDIO_LOOP_CROSSFADE_S", 2.0))

    # Po kolika sekundách nečinnosti smí controller shodit modelový kontejner.
    idle_unload_s: int = field(default_factory=lambda: _env_int("AUDIO_IDLE_UNLOAD_S", 900))

    api_key: str = field(default_factory=lambda: _env("AUDIO_API_KEY", ""))


CONFIG = Config()
