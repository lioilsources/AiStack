"""Předloha: dekódovat cokoli, vybrat nejreprezentativnější úsek, uložit.

Z jednoho uploadu vzniknou dva soubory, protože je model čte různě:

- `src.wav` — nejhlasitější úsek do 60 s. Z něj se analyzuje a na něm stojí
  cover (groove), který drží jeho rytmus a formu.
- `ref.wav` — **přesně** 30,000 s stereo 48 kHz. ACE-Step z reference audia
  bere tři 10s úseky (začátek, střed, konec) s náhodným posunem přes
  nezaseedovaný `random` (`process_reference_audio`). Posun je
  `randint(0, třetina − 10 s)`, takže je nulový jen tehdy, když má reference
  po převzorkování přesně 30 s — jinak by stejný seed nedal stejnou skladbu.
  Kratší předloha se proto dosmyčkuje sama, ne až v modelu.

Id předlohy je hash nahraných bajtů: tentýž soubor nahraný podruhé (retry
z telefonu po výpadku) se nezpracovává znovu.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from ..postproc import FFMPEG, FFmpegError, _run, duration_s

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
REF_SECONDS = 30
REF_FRAMES = REF_SECONDS * SAMPLE_RATE
MAX_SRC_SECONDS = 60.0
# Kratší předloha nenese dost na tempo ani na poslech LM.
MIN_SOURCE_SECONDS = 5.0
MAX_SOURCE_SECONDS = 20 * 60.0
MAX_UPLOAD_BYTES = 60 * 1024 * 1024

# Obálka pro výběr úseku: stačí hrubé rozlišení, jde o hlasitost, ne o obsah.
_ENV_RATE = 8_000
_ENV_FRAME_S = 0.5


class SampleError(ValueError):
    """Předloha je nepoužitelná (není to audio, je prázdná, moc dlouhá…)."""


@dataclass(frozen=True)
class SampleInfo:
    sample_id: str
    filename: str
    bytes: int
    source_duration_s: float
    # Úsek předlohy, který se opravdu použije (src.wav).
    window_start_s: float
    duration_s: float
    # Kde v src.wav začíná ref.wav; None = předloha kratší než 30 s, dosmyčkovaná.
    ref_start_s: float | None
    created_at: float


class SampleStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, sample_id: str) -> Path:
        if not sample_id or not all(c in "0123456789abcdef" for c in sample_id):
            raise SampleError("neplatné id předlohy")
        return self.root / sample_id

    def src(self, sample_id: str) -> Path:
        return self.dir(sample_id) / "src.wav"

    def ref(self, sample_id: str) -> Path:
        return self.dir(sample_id) / "ref.wav"

    def get(self, sample_id: str) -> SampleInfo | None:
        try:
            meta = self.dir(sample_id) / "meta.json"
        except SampleError:
            return None
        if not meta.is_file():
            return None
        return SampleInfo(**json.loads(meta.read_text()))

    def ingest(self, data: bytes, filename: str) -> SampleInfo:
        if not data:
            raise SampleError("prázdný soubor")
        if len(data) > MAX_UPLOAD_BYTES:
            raise SampleError(f"soubor je větší než {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        sample_id = hashlib.sha256(data).hexdigest()[:24]
        existing = self.get(sample_id)
        if existing is not None:
            return existing

        final = self.dir(sample_id)
        # Staví se vedle a přejmenuje až hotové: souběžný upload téhož souboru
        # ani pád uprostřed nenechá napůl zapsanou předlohu.
        tmp = Path(tempfile.mkdtemp(prefix=f".{sample_id}-", dir=self.root))
        try:
            original = tmp / ("original" + _safe_suffix(filename))
            original.write_bytes(data)
            info = _prepare(original, tmp, sample_id=sample_id, filename=filename, size=len(data))
            original.unlink()
            (tmp / "meta.json").write_text(json.dumps(asdict(info), ensure_ascii=False, indent=1))
            try:
                tmp.rename(final)
            except OSError:
                # Souběžný upload téhož souboru byl rychlejší — platí jeho výsledek.
                existing = self.get(sample_id)
                if existing is None:
                    raise
                return existing
        finally:
            # Po úspěšném přejmenování už tmp neexistuje a tohle nic neudělá.
            shutil.rmtree(tmp, ignore_errors=True)
        log.info(
            "předloha %s: %s, %.1f s → úsek %.1f–%.1f s",
            sample_id, filename, info.source_duration_s,
            info.window_start_s, info.window_start_s + info.duration_s,
        )
        return info

    # --- analýza patří k předloze, ne k jobu: generate si ji dohledá ---

    def save_analysis(self, sample_id: str, analysis: dict) -> None:
        path = self.dir(sample_id) / "analysis.json"
        path.write_text(json.dumps(analysis, ensure_ascii=False, indent=1))

    def analysis(self, sample_id: str) -> dict | None:
        path = self.dir(sample_id) / "analysis.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text())


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix.isascii() and suffix[1:].isalnum() and len(suffix) <= 6 else ".bin"


def _prepare(original: Path, out: Path, *, sample_id: str, filename: str, size: int) -> SampleInfo:
    try:
        probed = duration_s(original)
    except FFmpegError as exc:
        raise SampleError("soubor se nedá přečíst jako audio") from exc
    # Hrubá kontrola před dekódováním, ať se hodinový mix nedekóduje celý.
    if probed > MAX_SOURCE_SECONDS:
        raise SampleError(f"předloha je delší než {MAX_SOURCE_SECONDS / 60:.0f} min")

    env, total = _envelope(original)
    if env.size == 0 or float(env.max()) < 1e-4:
        raise SampleError("předloha je ticho")
    if total < MIN_SOURCE_SECONDS:
        raise SampleError(f"předloha je kratší než {MIN_SOURCE_SECONDS:.0f} s")

    window = min(MAX_SRC_SECONDS, total)
    start = loudest_window(env, _ENV_FRAME_S, window)
    src = out / "src.wav"
    _run([
        FFMPEG, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{window:.3f}",
        "-i", str(original), "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(src),
    ])
    src_len = duration_s(src)

    ref = out / "ref.wav"
    if src_len >= REF_SECONDS:
        # Nejhlasitějších 30 s uvnitř vybraného úseku, z téže obálky.
        first = int(round(start / _ENV_FRAME_S))
        sub = env[first : first + int(round(src_len / _ENV_FRAME_S))]
        ref_start = loudest_window(sub, _ENV_FRAME_S, REF_SECONDS)
        _exact_ref(["-ss", f"{ref_start:.3f}", "-i", str(src)], ref)
    else:
        ref_start = None
        _exact_ref(["-stream_loop", "-1", "-i", str(src)], ref)

    return SampleInfo(
        sample_id=sample_id,
        filename=Path(filename or "sample").name[:200],
        bytes=size,
        source_duration_s=round(total, 3),
        window_start_s=round(start, 3),
        duration_s=round(src_len, 3),
        ref_start_s=None if ref_start is None else round(ref_start, 3),
        created_at=time.time(),
    )


def _exact_ref(input_args: list[str], dst: Path) -> None:
    """Zapíše přesně REF_FRAMES vzorků (apad dorovná, atrim usekne)."""
    _run([
        FFMPEG, "-v", "error", "-y", *input_args,
        "-ac", "2", "-ar", str(SAMPLE_RATE),
        "-af", f"apad=whole_len={REF_FRAMES},atrim=end_sample={REF_FRAMES}",
        # Pojistka pro -stream_loop -1: nekonečný vstup nesmí viset.
        "-t", str(REF_SECONDS + 1),
        "-c:a", "pcm_s16le", str(dst),
    ])


def _envelope(path: Path) -> tuple[np.ndarray, float]:
    """RMS po půlsekundách z mono 8 kHz dekódu — levné i pro dlouhou předlohu.

    Vrací i délku dekódovaného signálu: ffprobe ji u VBR mp3 bez hlavičky jen
    odhaduje, počet vzorků je skutečný.
    """
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(_ENV_RATE),
         "-f", "s16le", "-"],
        capture_output=True, timeout=300, check=False,
    )
    if proc.returncode != 0:
        raise SampleError("soubor se nedá dekódovat jako audio")
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    seconds = pcm.size / _ENV_RATE
    hop = int(_ENV_RATE * _ENV_FRAME_S)
    n = pcm.size // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32), seconds
    frames = pcm[: n * hop].reshape(n, hop)
    return np.sqrt(np.mean(frames * frames, axis=1)), seconds


def loudest_window(env: np.ndarray, frame_s: float, window_s: float) -> float:
    """Začátek (v s) okna délky window_s s nejvyšší průměrnou energií.

    Energie, ne RMS: průměr kvadrátů preferuje hustý úsek (refrén, drop)
    před dlouhým tichým intrem, které má jen o málo nižší RMS.
    """
    width = max(1, int(round(window_s / frame_s)))
    if env.size <= width:
        return 0.0
    energy = np.concatenate([[0.0], np.cumsum(env.astype(np.float64) ** 2)])
    sums = energy[width:] - energy[:-width]
    return float(int(np.argmax(sums)) * frame_s)
