"""Co je na předloze slyšet: LM poslech (ACE-Step) + měření librosou.

5Hz LM (1.7B) předlohu zakóduje a „přečte" zpátky do popisu — caption,
žánr, tempo, tónina, takt, jazyk zpěvu. Caption a žánr umí dobře, čísla hůř:
tempo z LM se do DiT nikdy nepošle bez kontroly librosou (plán §5), protože
DiT ho bere doslova a špatné BPM je slyšet víc než špatný popis.

    python -m app.vibe.analyze sample.mp3     # JSON analýzy (LM přes AUDIO_MUSIC_URL)
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .prompt import clean_caption

log = logging.getLogger(__name__)

BPM_MIN, BPM_MAX = 40, 220
# Rozdíl LM proti librose, nad kterým se LM tempu nevěří (plán §3 krok 1).
BPM_TOLERANCE = 0.15
# Jak přesně musí sedět dvojnásobek/polovina, aby šlo o oktávovou záměnu.
OCTAVE_TOLERANCE = 0.06

VALID_TIME_SIGNATURES = {"2", "3", "4", "6"}

_PITCHES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# Krumhansl–Kessler profily tónin (1982).
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass
class SampleAnalysis:
    caption: str
    genre: str
    bpm: Optional[int]
    keyscale: str
    timesignature: str
    vocal_language: str
    instrumental: bool
    lyrics: str
    duration_s: float
    # Odkud která hodnota je: "lm" | "librosa" | "default" | "none".
    source: dict[str, str] = field(default_factory=dict)
    # Obě měření vedle sebe, ať je vidět, o čem se rozhodovalo.
    measured: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- librosa ---


def measure(path: Path) -> dict[str, Any]:
    """Tempo (autokorelace onsetů) a tónina (chroma + Krumhansl) z librosy.

    `feature.tempo`, ne `beat.beat_track`: tracker bere onsety jako medián přes
    frekvenční pásma a u úzkopásmové předlohy (klik, samotný bas) vidí ticho
    a vrátí 0. Na skutečné hudbě dávají obě totéž (lo-fi 161,5, synthwave
    112,3, kytara 95,7 — měřeno).
    """
    import librosa  # líně: import i první JIT numby trvají sekundy

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    if y.size == 0:
        return {}
    bpm = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key, confidence = estimate_key(chroma.mean(axis=1))
    return {
        "bpm": round(bpm, 1) if bpm > 0 else None,
        "keyscale": key,
        "key_confidence": round(confidence, 3),
    }


def warm_up() -> None:
    """Import librosy a JIT numby naprázdno (sekunda syntetického signálu)."""
    try:
        import librosa

        y = np.sin(2 * np.pi * 440 * np.arange(22050) / 22050).astype(np.float32)
        librosa.feature.tempo(y=y, sr=22050)
        librosa.feature.chroma_cqt(y=y, sr=22050)
    except Exception as exc:  # noqa: BLE001 — zahřátí nesmí shodit službu
        log.warning("vibe: zahřátí librosy selhalo: %s", exc)


def estimate_key(profile: np.ndarray) -> tuple[str, float]:
    """Tónina s nejvyšší korelací průměrného chroma s profily K–K.

    Jistota je korelace vítěze; paralelní a relativní tóniny (C dur / a moll)
    mívají korelace blízko sebe, takže pod ~0.6 je to spíš odhad.
    """
    best, best_r = "C major", -2.0
    if not np.any(profile):
        return best, 0.0
    for tonic in range(12):
        for mode, template in (("major", _MAJOR), ("minor", _MINOR)):
            r = float(np.corrcoef(profile, np.roll(template, tonic))[0, 1])
            if r > best_r:
                best, best_r = f"{_PITCHES[tonic]} {mode}", r
    return best, best_r


# --- sloučení ---


def pick_bpm(lm_bpm: Any, lib_bpm: Any) -> tuple[Optional[int], str, str]:
    """(bpm, zdroj, poznámka) podle pravidel plánu §3 krok 1.

    LM mimo 40–220 nebo chybějící → librosa. Obě a liší se o víc než 15 % →
    librosa. Výjimka je přesný dvojnásobek/polovina: beat tracker se chytá
    pulzu a jeho volba oktávy je daná apriorním rozdělením kolem 120 BPM,
    kdežto LM odhaduje *vnímané* tempo — a právě to DiT potřebuje.
    """
    lm = _as_number(lm_bpm)
    lib = _as_number(lib_bpm)
    lm_ok = lm is not None and BPM_MIN <= lm <= BPM_MAX
    lib_ok = lib is not None and lib > 0

    if not lm_ok and not lib_ok:
        return None, "none", "tempo se nepodařilo změřit"
    if not lm_ok:
        return _clamp_bpm(lib), "librosa", "" if lm is None else f"LM tempo {lm:g} mimo rozsah"
    if not lib_ok:
        return int(round(lm)), "lm", ""

    if abs(lm - lib) / lib <= BPM_TOLERANCE:
        return int(round(lm)), "lm", ""
    for factor in (2.0, 0.5):
        if abs(lm - lib * factor) / (lib * factor) <= OCTAVE_TOLERANCE:
            return int(round(lm)), "lm", f"librosa {lib:g} je oktávová záměna"
    return _clamp_bpm(lib), "librosa", f"LM {lm:g} a librosa {lib:g} se liší o víc než 15 %"


def _clamp_bpm(value: float) -> int:
    # Beat tracker umí vrátit i 250 u hustých hi-hatů; DiT bere 30–300,
    # rozsah plánu je užší a smysluplnější.
    v = value
    while v > BPM_MAX:
        v /= 2
    while v < BPM_MIN:
        v *= 2
    return int(round(v))


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


_KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(major|minor|maj|min|m)?\s*$", re.IGNORECASE)


def normalize_key(value: Any) -> str:
    """„a minor", „F#m", „Bb Major" → formát ACE-Stepu („A minor"); jinak ""."""
    if not isinstance(value, str):
        return ""
    m = _KEY_RE.match(value)
    if not m:
        return ""
    note, acc, mode = m.group(1).upper(), m.group(2), (m.group(3) or "major").lower()
    acc = {"♯": "#", "♭": "b"}.get(acc, acc)
    mode = "minor" if mode in ("minor", "min", "m") else "major"
    return f"{note}{acc} {mode}"


def is_instrumental(lyrics: Any) -> bool:
    """LM píše u instrumentálu `[Instrumental]`.

    Zpěv = v textu je aspoň jeden řádek, který není jen značka sekce
    ([Verse], [Chorus]…). Prázdný text s rozpoznaným jazykem je spíš
    instrumentál s vokálními samply než píseň.
    """
    text = lyrics if isinstance(lyrics, str) else ""
    sung = [
        line for line in text.splitlines()
        if line.strip() and not re.fullmatch(r"\s*\[[^\]]*\]\s*", line)
    ]
    return not sung


def merge(lm: dict[str, Any] | None, lib: dict[str, Any] | None, duration_s: float) -> SampleAnalysis:
    lm = lm or {}
    lib = lib or {}
    warnings: list[str] = []
    source: dict[str, str] = {}

    caption = clean_caption(str(lm.get("prompt") or lm.get("caption") or ""))
    source["caption"] = "lm" if caption else "none"
    if not caption:
        warnings.append("LM předlohu nepopsal — caption je potřeba napsat")

    bpm, source["bpm"], note = pick_bpm(lm.get("bpm"), lib.get("bpm"))
    if note:
        warnings.append(note)
        log.info("vibe tempo: %s (LM=%s, librosa=%s)", note, lm.get("bpm"), lib.get("bpm"))

    lm_key = normalize_key(lm.get("keyscale"))
    if lm_key:
        keyscale, source["keyscale"] = lm_key, "lm"
    elif lib.get("keyscale"):
        keyscale, source["keyscale"] = str(lib["keyscale"]), "librosa"
    else:
        keyscale, source["keyscale"] = "", "none"

    ts = str(lm.get("timesignature") or "").strip()
    ts = ts.split("/")[0] if "/" in ts else ts
    if ts in VALID_TIME_SIGNATURES:
        source["timesignature"] = "lm"
    else:
        ts, source["timesignature"] = "4", "default"

    lyrics = str(lm.get("lyrics") or "")
    instrumental = is_instrumental(lyrics)
    source["instrumental"] = "lm" if lm else "default"
    language = str(lm.get("language") or "unknown").strip().lower() or "unknown"

    return SampleAnalysis(
        caption=caption,
        genre=str(lm.get("genre") or lm.get("genres") or "").strip(),
        bpm=bpm,
        keyscale=keyscale,
        timesignature=ts,
        vocal_language="unknown" if instrumental else language,
        instrumental=instrumental,
        lyrics="" if instrumental else lyrics.strip(),
        duration_s=round(duration_s, 3),
        source=source,
        measured={
            "lm_bpm": lm.get("bpm"),
            "librosa_bpm": lib.get("bpm"),
            "lm_keyscale": lm.get("keyscale"),
            "librosa_keyscale": lib.get("keyscale"),
            "librosa_key_confidence": lib.get("key_confidence"),
        },
        warnings=warnings,
    )


def analyze(
    path: Path,
    duration_s: float,
    listen: Callable[[Path], dict[str, Any]] | None,
) -> SampleAnalysis:
    """LM poslech + librosa; výpadek jednoho z nich analýzu neshodí.

    Bez LM zůstane prázdný caption (uživatel ho dopíše), bez librosy jen
    neověřené tempo — obojí je lepší než žádná analýza.
    """
    lm: dict[str, Any] = {}
    errors: list[str] = []
    if listen is not None:
        try:
            lm = listen(path)
        except Exception as exc:  # noqa: BLE001 — model může spadnout jakkoli
            log.warning("vibe: LM analýza selhala: %s", exc)
            errors.append(f"LM analýza selhala: {exc}")
    try:
        lib = measure(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("vibe: librosa selhala: %s", exc)
        errors.append(f"měření tempa selhalo: {exc}")
        lib = {}
    if not lm and not lib:
        raise RuntimeError("; ".join(errors) or "analýza nic nevrátila")
    result = merge(lm, lib, duration_s)
    result.warnings = errors + result.warnings
    return result


def _main() -> int:
    import argparse
    import json
    import os
    import tempfile

    from ..backends.acestep import AceStepBackend
    from .sample import SampleStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample")
    parser.add_argument("--no-lm", action="store_true", help="jen librosa")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        store = SampleStore(Path(tmp))
        data = Path(args.sample).read_bytes()
        info = store.ingest(data, Path(args.sample).name)
        listen = None
        if not args.no_lm:
            url = os.environ.get("AUDIO_MUSIC_URL", "http://127.0.0.1:8094")
            listen = AceStepBackend(url, "acestep-v15-turbo", 300.0).analyze
        result = analyze(store.src(info.sample_id), info.duration_s, listen)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
