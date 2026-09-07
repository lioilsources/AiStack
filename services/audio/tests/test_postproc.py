"""Post-processing musí sedět v délce, hlasitosti a formátu — na tom stojí QA."""

from __future__ import annotations

import subprocess

import pytest

from app import postproc
from tests.conftest import make_tone


def test_measure_loudness_short_clip(tmp_path):
    """Půlsekundový klip nesmí měření shodit — přesně tam je většina SFX."""
    clip = make_tone(tmp_path / "short.wav", seconds=0.5)
    loudness = postproc.measure_loudness(clip)
    assert -40 < loudness.integrated_lufs < 0
    assert loudness.true_peak_db < 6


def test_loopify_shortens_by_crossfade(tmp_path):
    src = make_tone(tmp_path / "src.wav", seconds=8.0)
    dst = tmp_path / "loop.wav"
    postproc.loopify(src, dst, crossfade_s=2.0)
    assert postproc.duration_s(dst) == pytest.approx(6.0, abs=0.1)


def test_loopify_caps_crossfade_at_quarter_of_track(tmp_path):
    """Na krátké stopě se prolnutí zkrátí, aby nesežralo obsah.

    Sekundová stopa s požadovaným prolnutím 2 s dostane prolnutí 0,25 s,
    takže výstup je 0,75 s — ne prázdno.
    """
    src = make_tone(tmp_path / "tiny.wav", seconds=1.0)
    dst = tmp_path / "loop.wav"
    postproc.loopify(src, dst, crossfade_s=2.0)
    assert postproc.duration_s(dst) == pytest.approx(0.75, abs=0.1)


def test_loopify_seam_is_continuous(tmp_path):
    """Konec smyčky musí navazovat na začátek — jinak je ve hře slyšet lupnutí.

    Měří se rozdíl mezi posledním vzorkem a prvním; u spojité smyčky je
    v řádu jednotlivých vzorků sinusovky, u tvrdého střihu skočí.
    """
    src = make_tone(tmp_path / "src.wav", seconds=8.0, freq=100)
    dst = tmp_path / "loop.wav"
    postproc.loopify(src, dst, crossfade_s=2.0)
    samples = _read_samples(dst)
    assert abs(samples[-1] - samples[0]) < 0.15


def _read_samples(path):
    """Načte mono float vzorky přes ffmpeg (bez numpy/soundfile)."""
    import array
    import subprocess

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
         "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    buf = array.array("f")
    buf.frombytes(raw)
    return buf


def test_trim_removes_leading_and_trailing_silence(tmp_path):
    src = make_tone(tmp_path / "padded.wav", seconds=2.0, silence_pad=1.0)
    assert postproc.duration_s(src) == pytest.approx(4.0, abs=0.1)
    dst = tmp_path / "trimmed.wav"
    postproc.trim_silence(src, dst)
    assert postproc.duration_s(dst) == pytest.approx(2.0, abs=0.2)


def test_process_music_hits_loudness_target(tmp_path):
    src = make_tone(tmp_path / "src.wav", seconds=8.0)
    result = postproc.process(
        src, tmp_path / "out.ogg",
        fmt="ogg", target_lufs=-16.0, true_peak_db=-1.0,
        mono=False, trim=False, loop=True, crossfade_s=2.0,
    )
    assert result.path.exists()
    assert result.duration == pytest.approx(6.0, abs=0.2)
    # Těsná tolerance schválně: s auto-levelem v alimiteru vycházel cíl −16
    # konzistentně na −15, což by prošlo volnější mezí a nikdo by si nevšiml.
    assert result.loudness_lufs == pytest.approx(-16.0, abs=0.7)
    assert result.true_peak_db <= -0.5


def test_process_sfx_is_mono(tmp_path):
    src = make_tone(tmp_path / "src.wav", seconds=1.0, silence_pad=0.5)
    result = postproc.process(
        src, tmp_path / "out.ogg",
        fmt="ogg", target_lufs=-18.0, true_peak_db=-1.0,
        mono=True, trim=True, loop=False, crossfade_s=2.0,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(result.path)],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "1"
    assert result.duration == pytest.approx(1.0, abs=0.3)


def test_ffmpeg_error_surfaces(tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    with pytest.raises(postproc.FFmpegError):
        postproc.duration_s(broken)


def test_process_sfx_is_capped_at_requested_length(tmp_path):
    """MOSS negeneruje pod sekundu, takže půlsekundový efekt musí uříznout post-proc."""
    src = make_tone(tmp_path / "src.wav", seconds=1.0)
    result = postproc.process(
        src, tmp_path / "out.ogg",
        fmt="ogg", target_lufs=-18.0, true_peak_db=-1.0,
        mono=True, trim=True, loop=False, crossfade_s=2.0,
        max_duration_s=0.5,
    )
    assert result.duration == pytest.approx(0.5, abs=0.06)


def test_process_does_not_pad_short_sfx(tmp_path):
    """Kratší efekt se nedoplňuje — 0,3 s zůstane 0,3 s."""
    src = make_tone(tmp_path / "src.wav", seconds=0.3)
    result = postproc.process(
        src, tmp_path / "out.ogg",
        fmt="ogg", target_lufs=-18.0, true_peak_db=-1.0,
        mono=True, trim=True, loop=False, crossfade_s=2.0,
        max_duration_s=0.5,
    )
    assert result.duration == pytest.approx(0.3, abs=0.06)
