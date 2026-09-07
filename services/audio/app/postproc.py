"""Post-processing přes ffmpeg: loudness, trim, seamless loop, převod formátu.

Model vrátí WAV o neurčité hlasitosti a s náběhem/doběhem ticha. Hra potřebuje
OGG Vorbis o předvídatelné hlasitosti, SFX mono a hudbu, která se dá zacyklit
bez slyšitelného švu. Všechno tady je čistě ffmpeg — žádný numpy, aby image
služby zůstal malý a nezávislý na torch stacku.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class FFmpegError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise FFmpegError(f"{args[0]} selhal ({proc.returncode}): " + " | ".join(tail))
    return proc


def duration_s(path: Path) -> float:
    proc = _run(
        [
            FFPROBE, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        timeout=60,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:  # pragma: no cover - ffprobe vrací číslo nebo padá
        raise FFmpegError(f"ffprobe nevrátil délku pro {path}: {proc.stdout!r}") from exc


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float
    true_peak_db: float
    lra: float


def measure_loudness(path: Path) -> Loudness:
    """Změří EBU R128 přes loudnorm v měřicím režimu.

    Krátké SFX se před měřením doplní tichem na 3 s: loudnorm pod tuto délku
    měření odmítne. Integrovaná hlasitost je gateovaná, takže přidané ticho
    výsledek neposune — jen umlčí varování a dá reprodukovatelné číslo.
    """
    args = [
        FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
        "-af", "apad=whole_dur=3,loudnorm=print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise FFmpegError("měření loudness selhalo: " + (proc.stderr or "")[-400:])
    payload = _last_json_object(proc.stderr or "")
    if payload is None:
        raise FFmpegError("loudnorm nevrátil JSON")
    return Loudness(
        integrated_lufs=_to_float(payload.get("input_i"), -70.0),
        true_peak_db=_to_float(payload.get("input_tp"), -99.0),
        lra=_to_float(payload.get("input_lra"), 0.0),
    )


def _to_float(value: object, fallback: float) -> float:
    try:
        f = float(str(value))
    except (TypeError, ValueError):
        return fallback
    return fallback if math.isinf(f) or math.isnan(f) else f


def _last_json_object(text: str) -> Optional[dict]:
    """Vytáhne poslední {...} blok z ffmpeg stderr."""
    end = text.rfind("}")
    while end != -1:
        start = text.rfind("{", 0, end)
        while start != -1:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                start = text.rfind("{", 0, start)
        end = text.rfind("}", 0, end)
    return None


def trim_silence(src: Path, dst: Path, threshold_db: float = -50.0) -> None:
    """Ořízne ticho na začátku i na konci (peak detekce).

    Druhý silenceremove běží na obráceném signálu — ffmpeg umí ořezávat jen
    zepředu, takže konec se řeší areverse tam a zpět.
    """
    flt = (
        f"silenceremove=start_periods=1:start_silence=0:start_threshold={threshold_db}dB:detection=peak,"
        "areverse,"
        f"silenceremove=start_periods=1:start_silence=0:start_threshold={threshold_db}dB:detection=peak,"
        "areverse"
    )
    _run([FFMPEG, "-hide_banner", "-nostats", "-y", "-i", str(src), "-af", flt, str(dst)])


def limit_duration(src: Path, dst: Path, max_s: float, fade_s: float = 0.01) -> bool:
    """Zkrátí stopu na max_s s krátkým doběhem; vrátí, jestli se zkracovalo.

    MOSS negeneruje kratší klip než sekundu, takže půlsekundový výstřel vyjde
    dvakrát delší, než se žádá — a při rychlé palbě se pak efekty překrývají.
    Doběh 10 ms je tam proto, aby střih nelupl.
    """
    total = duration_s(src)
    if total <= max_s + 0.01:
        return False
    fade = min(fade_s, max_s / 4.0)
    _run([
        FFMPEG, "-v", "error", "-y", "-i", str(src),
        "-af", f"atrim=0:{max_s:.3f},asetpts=PTS-STARTPTS,"
               f"afade=t=out:st={max(0.0, max_s - fade):.3f}:d={fade:.3f}",
        str(dst),
    ])
    return True


def loopify(src: Path, dst: Path, crossfade_s: float) -> None:
    """Udělá ze stopy bezešvou smyčku složením ocasu přes hlavu.

    Výstup má délku D − C. Konec výstupu je původní vzorek na čase D − C,
    což je přesně místo, odkud začíná přeložený ocas — přechod z konce zpátky
    na začátek proto nemá sešívací lupnutí.

    Jede to přes dočasné soubory, ne přes jeden filter_complex. Varianta
    `[0:a]asplit=3` + `acrossfade`/`amix` + `concat` na ffmpeg 6 tiše vrací
    jen `body` a prolnutí zahodí (změřeno: 8s stopa, prolnutí 2 s → 4 s místo
    6 s, bez jediné chyby na stderr) — concat čte segmenty popořadě, takže
    větev s ocasem nikdy nedostane data. Čtyři průchody navíc jsou levnější
    než ticho tam, kde má být přechod.
    """
    total = duration_s(src)
    xf = min(crossfade_s, max(0.1, total / 4.0))
    body_end = total - xf
    if body_end <= xf:
        # Stopa je kratší než dvojnásobek prolnutí — smyčka by sežrala obsah.
        shutil.copyfile(src, dst)
        return

    with tempfile.TemporaryDirectory(prefix="audio-loop-") as tmpdir:
        tmp = Path(tmpdir)
        head, body, tail, faded = tmp / "head.wav", tmp / "body.wav", tmp / "tail.wav", tmp / "xf.wav"

        _run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-af",
              f"atrim=0:{xf:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={xf:.3f}:curve=tri", str(head)])
        _run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-af",
              f"atrim={xf:.3f}:{body_end:.3f},asetpts=PTS-STARTPTS", str(body)])
        _run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-af",
              f"atrim={body_end:.3f}:{total:.3f},asetpts=PTS-STARTPTS,"
              f"afade=t=out:st=0:d={xf:.3f}:curve=tri", str(tail)])
        # normalize=0: trojúhelníkové náběhy se sčítají na jedničku, takže
        # prolnutí drží hlasitost. S normalizací by v něm byl propad o 6 dB.
        _run([FFMPEG, "-v", "error", "-y", "-i", str(head), "-i", str(tail),
              "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=shortest:normalize=0[x]",
              "-map", "[x]", str(faded)])
        _run([FFMPEG, "-v", "error", "-y", "-i", str(faded), "-i", str(body),
              "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[o]", "-map", "[o]", str(dst)])


def encode(
    src: Path,
    dst: Path,
    *,
    fmt: str,
    gain_db: float,
    limit_db: float,
    mono: bool,
    sample_rate: int = 44100,
) -> None:
    """Aplikuje zisk + limiter a zapíše cílový formát."""
    # level=disabled je povinné: alimiter má auto-level zapnutý a po limitování
    # signál zase vytáhne tak, aby špička seděla na stropě — čímž přepíše celou
    # normalizaci. Měřeno: cíl −16 LUFS vycházel u všech stop shodně na −15,0.
    chain = [
        f"volume={gain_db:.2f}dB",
        f"alimiter=limit={_db_to_linear(limit_db):.6f}:level=disabled",
    ]
    chain.append(f"aresample={sample_rate}")

    args = [FFMPEG, "-hide_banner", "-nostats", "-y", "-i", str(src), "-af", ",".join(chain)]
    # Downmix přes -ac, ne přes pan: pan=mono|c0=…c1 spadne na mono vstupu,
    # a jestli model vrátí mono nebo stereo, se dopředu spolehnout nedá.
    if mono:
        args += ["-ac", "1"]
    if fmt == "ogg":
        args += ["-c:a", "libvorbis", "-q:a", str(_ogg_quality)]
    elif fmt == "mp3":
        args += ["-c:a", "libmp3lame", "-q:a", "2"]
    elif fmt == "flac":
        args += ["-c:a", "flac"]
    elif fmt == "wav":
        args += ["-c:a", "pcm_s16le"]
    else:
        raise ValueError(f"neznámý formát {fmt!r}")
    args.append(str(dst))
    _run(args)


_ogg_quality = 6


def set_ogg_quality(q: int) -> None:
    global _ogg_quality
    _ogg_quality = q


def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


@dataclass(frozen=True)
class Processed:
    path: Path
    duration: float
    loudness_lufs: float
    true_peak_db: float


def process(
    raw: Path,
    dst: Path,
    *,
    fmt: str,
    target_lufs: float,
    true_peak_db: float,
    mono: bool,
    trim: bool,
    loop: bool,
    crossfade_s: float,
    max_duration_s: float | None = None,
    sample_rate: int = 44100,
) -> Processed:
    """Celý řetěz: trim → loop → normalizace → převod, a změří výsledek.

    Pořadí je podstatné: ořez musí přijít před zacyklením, jinak se ticho
    z konce stopy přeloží na začátek smyčky a šev je slyšet.

    Normalizace je zisk spočítaný z jednoho měření, ne dvouprůchodový loudnorm:
    loudnorm mění dynamiku a u půlsekundových SFX si s gatováním neporadí,
    kdežto prostý posun hlasitosti s limiterem dá stejný cíl a nechá transient
    na pokoji — a právě transient je u herních SFX to jediné, na čem záleží.
    """
    with tempfile.TemporaryDirectory(prefix="audio-post-") as tmpdir:
        tmp = Path(tmpdir)
        cur = raw

        if trim:
            trimmed = tmp / "trimmed.wav"
            trim_silence(cur, trimmed)
            if duration_s(trimmed) > 0.05:
                cur = trimmed
            else:
                log.warning("trim by smazal celý signál, nechávám původní stopu")

        if max_duration_s:
            limited = tmp / "limited.wav"
            if limit_duration(cur, limited, max_duration_s):
                cur = limited

        if loop:
            looped = tmp / "looped.wav"
            loopify(cur, looped, crossfade_s)
            cur = looped

        meas = measure_loudness(cur)
        gain = target_lufs - meas.integrated_lufs
        # Zisk mimo tenhle rozsah znamená, že měření selhalo (skoro ticho nebo
        # klip), a zesílit o 40 dB šum je horší než nechat stopu, jak je.
        gain = max(-30.0, min(30.0, gain))

        dst.parent.mkdir(parents=True, exist_ok=True)
        encode(
            cur, dst,
            fmt=fmt, gain_db=gain, limit_db=true_peak_db,
            mono=mono, sample_rate=sample_rate,
        )

    final = measure_loudness(dst)
    return Processed(
        path=dst,
        duration=duration_s(dst),
        loudness_lufs=final.integrated_lufs,
        true_peak_db=final.true_peak_db,
    )
