import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_tone(path: Path, seconds: float = 4.0, freq: int = 440, silence_pad: float = 0.0) -> Path:
    """Vyrobí testovací WAV: volitelně ticho, tón, ticho."""
    if silence_pad > 0:
        flt = (
            f"sine=frequency={freq}:duration={seconds}[t];"
            f"anullsrc=r=44100:cl=stereo,atrim=0:{silence_pad}[s1];"
            f"anullsrc=r=44100:cl=stereo,atrim=0:{silence_pad}[s2];"
            f"[s1][t][s2]concat=n=3:v=0:a=1[out]"
        )
        args = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-filter_complex", flt, "-map", "[out]", str(path)]
    else:
        args = [
            "ffmpeg", "-hide_banner", "-nostats", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-ac", "2", str(path),
        ]
    subprocess.run(args, check=True, capture_output=True)
    return path


@pytest.fixture
def tone(tmp_path):
    return make_tone(tmp_path / "tone.wav")


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUDIO_DB_PATH", str(tmp_path / "data" / "jobs.db"))
    os.makedirs(tmp_path / "data", exist_ok=True)
