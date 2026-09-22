"""Vibe z předlohy — bez GPU: předloha, pravidla analýzy, caption, API.

Model zastupuje falešný backend s `analyze()`; librosa běží doopravdy nad
syntetickým signálem, protože pravidla pro tempo stojí na tom, co librosa
skutečně vrací (oktávové záměny), ne na tom, co by vracet měla.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app.backends.base import GenSpec, RawAudio
from app.vibe import analyze as vibe_analyze
from app.vibe.analyze import estimate_key, is_instrumental, merge, normalize_key, pick_bpm
from app.vibe.generate import build_spec, resolve
from app.vibe.prompt import build_caption, clean_caption
from app.vibe.sample import REF_FRAMES, SampleError, SampleStore, loudest_window
from tests.conftest import make_tone

LM_LOFI = {
    # Doslova tvar, který vrátil ACE-Step 1.5 (full_analysis_only) na SPARKu.
    "status_message": "Full Hardware Analysis Success",
    "bpm": 81,
    "keyscale": "E major",
    "timesignature": "4",
    "duration": 29,
    "genre": "Lo-fi hip hop",
    "prompt": "A classic lo-fi hip-hop instrumental built on a dusty, sampled drum break "
    "and a warm, round bassline that provides a steady, head-nodding groove.",
    "lyrics": "[Instrumental]",
    "language": "unknown",
}


def make_clicks(path: Path, bpm: float, seconds: float = 20.0, sr: int = 44100) -> Path:
    """Metronom: krátké klepnutí na každou dobu, stereo WAV."""
    y = np.zeros(int(seconds * sr), dtype=np.float32)
    click = np.hanning(int(0.02 * sr)).astype(np.float32) * np.sin(
        2 * np.pi * 1000 * np.arange(int(0.02 * sr)) / sr
    ).astype(np.float32)
    step = 60.0 / bpm
    t = 0.0
    while t < seconds - 0.05:
        i = int(t * sr)
        y[i : i + click.size] += click[: y.size - i]
        t += step
    sf.write(str(path), np.stack([y, y], axis=1) * 0.8, sr)
    return path


def make_quiet_then_loud(path: Path) -> Path:
    """10 s ticha tónu, pak 80 s hlasitě — nejhlasitější okno začíná po 10 s."""
    sr = 44100
    t = np.arange(90 * sr) / sr
    y = np.sin(2 * np.pi * 220 * t).astype(np.float32)
    y[: 10 * sr] *= 0.01
    sf.write(str(path), np.stack([y, y], axis=1) * 0.5, sr)
    return path


# --- předloha ---


def test_ingest_picks_loudest_window_and_exact_reference(tmp_path):
    store = SampleStore(tmp_path / "samples")
    src = make_quiet_then_loud(tmp_path / "long.wav")
    info = store.ingest(src.read_bytes(), "long.wav")

    assert info.source_duration_s == pytest.approx(90, abs=0.1)
    assert info.duration_s == pytest.approx(60, abs=0.1)
    assert info.window_start_s >= 9.5  # tiché intro se přeskočí
    ref = sf.info(str(store.ref(info.sample_id)))
    # Přesně 30 s při 48 kHz — jinak ACE-Step vybere úseky reference náhodně.
    assert (ref.frames, ref.samplerate, ref.channels) == (REF_FRAMES, 48000, 2)
    src_info = sf.info(str(store.src(info.sample_id)))
    assert (src_info.samplerate, src_info.channels) == (48000, 2)


def test_short_sample_is_looped_to_exact_reference(tmp_path):
    store = SampleStore(tmp_path / "samples")
    tone = make_tone(tmp_path / "short.wav", seconds=12.0)
    info = store.ingest(tone.read_bytes(), "short.wav")
    assert info.duration_s == pytest.approx(12.0, abs=0.1)
    assert info.ref_start_s is None
    assert sf.info(str(store.ref(info.sample_id))).frames == REF_FRAMES


def test_same_upload_is_not_processed_twice(tmp_path):
    store = SampleStore(tmp_path / "samples")
    data = make_tone(tmp_path / "t.wav", seconds=8.0).read_bytes()
    first = store.ingest(data, "a.wav")
    mtime = store.src(first.sample_id).stat().st_mtime_ns
    again = store.ingest(data, "jine-jmeno.wav")
    assert again == first
    assert store.src(first.sample_id).stat().st_mtime_ns == mtime


@pytest.mark.parametrize(
    "data, match",
    [
        (b"", "prázdný"),
        (b"this is not audio at all" * 100, "audio"),
    ],
)
def test_ingest_rejects_garbage(tmp_path, data, match):
    with pytest.raises(SampleError, match=match):
        SampleStore(tmp_path / "s").ingest(data, "x.mp3")


def test_ingest_rejects_silence_and_tiny_clips(tmp_path):
    store = SampleStore(tmp_path / "s")
    silence = tmp_path / "silence.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "10", str(silence)],
        check=True,
    )
    with pytest.raises(SampleError, match="ticho"):
        store.ingest(silence.read_bytes(), "silence.wav")
    with pytest.raises(SampleError, match="kratší"):
        store.ingest(make_tone(tmp_path / "tiny.wav", seconds=2.0).read_bytes(), "tiny.wav")


def test_ingest_leaves_no_temp_dirs(tmp_path):
    store = SampleStore(tmp_path / "s")
    with pytest.raises(SampleError):
        store.ingest(b"garbage" * 1000, "x.mp3")
    store.ingest(make_tone(tmp_path / "t.wav", seconds=6.0).read_bytes(), "t.wav")
    assert [p.name for p in (tmp_path / "s").iterdir() if p.name.startswith(".")] == []


def test_sample_id_is_validated(tmp_path):
    store = SampleStore(tmp_path / "s")
    assert store.get("../../etc") is None
    with pytest.raises(SampleError):
        store.src("../../etc/passwd")


def test_loudest_window_prefers_dense_section():
    env = np.array([0.1] * 20 + [0.9] * 10 + [0.1] * 20, dtype=np.float32)
    assert loudest_window(env, 0.5, 5.0) == pytest.approx(10.0)
    assert loudest_window(env, 0.5, 100.0) == 0.0  # okno delší než předloha


# --- tempo, tónina, zpěv ---


@pytest.mark.parametrize(
    "lm, lib, expected, source",
    [
        (81, 161.5, 81, "lm"),  # naměřeno na lo-fi předloze: librosa chytila dvojnásobek
        (120, 60.2, 120, "lm"),  # a opačná oktáva
        (112, 112.3, 112, "lm"),  # shoda v toleranci → LM
        (140, 96.0, 96, "librosa"),  # rozdíl > 15 % bez oktávy → librosa
        (None, 95.7, 96, "librosa"),
        (300, 150.0, 150, "librosa"),  # LM mimo 40–220
        (90, None, 90, "lm"),
        ("N/A", 250.0, 125, "librosa"),  # beat tracker nad rozsahem se půlí
    ],
)
def test_pick_bpm_rules(lm, lib, expected, source):
    bpm, got_source, _ = pick_bpm(lm, lib)
    assert (bpm, got_source) == (expected, source)


def test_pick_bpm_without_any_measurement():
    assert pick_bpm(None, None)[:2] == (None, "none")


def test_librosa_measures_click_track_tempo(tmp_path):
    clicks = make_clicks(tmp_path / "clicks.wav", bpm=100)
    measured = vibe_analyze.measure(clicks)
    bpm = measured["bpm"]
    # Oktávu beat tracker volit smí (proto pravidlo výš), tempo ne.
    assert any(abs(bpm - 100 * f) / (100 * f) < 0.05 for f in (0.5, 1, 2)), bpm


def test_estimate_key_from_triad_profile():
    profile = np.zeros(12)
    profile[[0, 4, 7]] = 1.0  # C E G
    assert estimate_key(profile)[0] == "C major"
    profile = np.zeros(12)
    profile[[9, 0, 4]] = 1.0  # A C E
    assert estimate_key(profile)[0] == "A minor"


@pytest.mark.parametrize(
    "raw, expected",
    [("E major", "E major"), ("a minor", "A minor"), ("F#m", "F# minor"), ("Bb Major", "Bb major"),
     ("C♯ minor", "C# minor"), ("N/A", ""), (None, ""), ("H dur", "")],
)
def test_normalize_key(raw, expected):
    assert normalize_key(raw) == expected


def test_instrumental_detection():
    assert is_instrumental("[Instrumental]")
    assert is_instrumental("")
    assert is_instrumental("[Intro]\n[Outro]")
    assert not is_instrumental("[Verse]\nwalking down the street")


def test_merge_uses_lm_caption_and_checked_tempo():
    a = merge(LM_LOFI, {"bpm": 161.5, "keyscale": "A minor", "key_confidence": 0.65}, 28.5)
    assert a.bpm == 81 and a.source["bpm"] == "lm"
    assert a.keyscale == "E major" and a.source["keyscale"] == "lm"
    assert a.timesignature == "4"
    assert a.instrumental and a.vocal_language == "unknown" and a.lyrics == ""
    assert a.caption.startswith("A classic lo-fi")
    assert a.measured["librosa_bpm"] == 161.5
    assert any("oktávová" in w for w in a.warnings)


def test_merge_without_lm_falls_back_to_librosa():
    a = merge({}, {"bpm": 95.7, "keyscale": "G major"}, 30.0)
    assert (a.bpm, a.keyscale, a.timesignature) == (96, "G major", "4")
    assert a.source == {
        "caption": "none", "bpm": "librosa", "keyscale": "librosa",
        "timesignature": "default", "instrumental": "default",
    }
    assert a.caption == "" and any("caption" in w for w in a.warnings)


def test_analyze_survives_lm_failure(tmp_path):
    clicks = make_clicks(tmp_path / "c.wav", bpm=120)

    def broken(_: Path) -> dict:
        raise RuntimeError("CUDA out of memory")

    result = vibe_analyze.analyze(clicks, 20.0, broken)
    assert result.bpm is not None
    assert any("CUDA out of memory" in w for w in result.warnings)


# --- caption ---


def test_clean_caption_strips_filler_and_markdown():
    assert clean_caption("This audio contains a **dusty** drum break.") == "A dusty drum break."
    assert clean_caption("The song is an upbeat tune. The track is a remix.") == (
        "An upbeat tune. A remix."
    )
    # Uprostřed věty zůstává.
    assert clean_caption("A song where the audio is warm.") == "A song where the audio is warm."


def test_build_caption_appends_hint():
    assert build_caption("Warm lo-fi groove", "more energetic, add strings") == (
        "Warm lo-fi groove. More energetic, add strings"
    )
    assert build_caption("Warm lo-fi groove.", "") == "Warm lo-fi groove."
    assert build_caption("", "dark techno") == "dark techno"


# --- parametry generování ---


def _sample_info(duration: float = 28.5):
    from app.vibe.sample import SampleInfo

    return SampleInfo(
        sample_id="ab" * 12, filename="lofi.mp3", bytes=1, source_duration_s=duration,
        window_start_s=0.0, duration_s=duration, ref_start_s=None, created_at=0.0,
    )


def test_resolve_prefers_request_over_analysis():
    analysis = merge(LM_LOFI, {"bpm": 161.5}, 28.5).to_dict()
    p = resolve({"caption": "Dark trap beat", "bpm": 140, "duration_s": 60}, analysis, _sample_info())
    assert (p.caption, p.bpm, p.keyscale, p.duration_s) == ("Dark trap beat", 140, "E major", 60)
    assert p.mode == "vibe" and p.cover_strength is None


def test_resolve_groove_uses_sample_length():
    analysis = merge(LM_LOFI, {}, 28.5).to_dict()
    p = resolve({"mode": "groove", "duration_s": 120, "cover_strength": 0.25}, analysis, _sample_info())
    assert p.duration_s == 28.5 and p.cover_strength == 0.25


def test_resolve_needs_some_caption():
    with pytest.raises(ValueError, match="caption"):
        resolve({}, None, _sample_info())


def test_build_spec_modes(tmp_path):
    store = SampleStore(tmp_path)
    analysis = merge(LM_LOFI, {}, 28.5).to_dict()
    sid = "ab" * 12
    vibe = build_spec(resolve({}, analysis, _sample_info()), store, sid, seed=5)
    assert vibe.task_type == "text2music"
    assert vibe.reference_audio == store.ref(sid) and vibe.src_audio is None
    assert vibe.rewrite_caption is False and vibe.key == "E major" and vibe.bpm == 81
    assert vibe.thinking is False  # LM plán je volitelný (nejde zopakovat)
    planned = build_spec(resolve({"lm_plan": True}, analysis, _sample_info()), store, sid, seed=5)
    assert planned.thinking is True

    groove = build_spec(resolve({"mode": "groove", "lm_plan": True}, analysis, _sample_info()), store, sid, seed=5)
    assert groove.task_type == "cover" and groove.cover_strength == 0.5
    assert groove.thinking is False  # cover LM nepoužívá
    assert groove.src_audio == store.src(sid) and groove.reference_audio is None


# --- API ---


class VibeBackend:
    kind = "music"
    name = "fake-acestep"

    def __init__(self) -> None:
        self.calls: list[GenSpec] = []
        self.listened: list[Path] = []

    def generate(self, spec: GenSpec, workdir: Path) -> RawAudio:
        self.calls.append(spec)
        path = make_tone(workdir / "raw.wav", seconds=max(1.0, min(spec.duration_s, 12.0)))
        info = {"dit_model": "acestep-v15-turbo", "lm_model": "acestep-5Hz-lm-1.7B"}
        return RawAudio(path=path, seed=spec.seed, model="acestep-v15-turbo", info=info)

    def analyze(self, audio: Path) -> dict:
        self.listened.append(audio)
        return dict(LM_LOFI)

    def health(self) -> tuple[bool, str]:
        return True, "fake"

    def loaded_models(self) -> list[str]:
        return ["acestep-v15-turbo"]


@pytest.fixture
def client(monkeypatch):
    from app import main as main_mod

    from tests.test_api import FakeBackend

    music = VibeBackend()
    monkeypatch.setattr(main_mod, "AceStepBackend", lambda *a, **k: music)
    monkeypatch.setattr(main_mod, "SfxHTTPBackend", lambda *a, **k: FakeBackend("sfx"))
    with TestClient(main_mod.app) as c:
        c.music = music  # type: ignore[attr-defined]
        yield c


def _await(client, job_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/audio/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} nedoběhl")


def _upload(client, path: Path) -> dict:
    with path.open("rb") as fh:
        resp = client.post("/v1/audio/vibe/samples", files={"sample": (path.name, fh, "audio/wav")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_vibe_flow_end_to_end(client, tmp_path):
    sample = _upload(client, make_clicks(tmp_path / "lofi.wav", bpm=82, seconds=20))
    assert sample["analysis"] is None

    resp = client.post("/v1/audio/vibe/analyze", json={"sample_id": sample["sample_id"]})
    assert resp.status_code == 202
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["task"] == "analyze" and job["outputs"] == []
    analysis = job["result"]
    assert analysis["caption"].startswith("A classic lo-fi") and analysis["bpm"] == 81
    assert client.music.listened[-1].name == "src.wav"

    # Po reuploadu je analýza k dispozici bez nového jobu.
    assert _upload(client, tmp_path / "lofi.wav")["analysis"]["bpm"] == 81

    resp = client.post("/v1/audio/vibe/generate", json={
        "sample_id": sample["sample_id"], "user_hint": "more cinematic", "seed": 40, "variations": 2,
    })
    assert resp.status_code == 202, resp.text
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["task"] == "vibe"

    spec = client.music.calls[-1]
    assert spec.task_type == "text2music" and spec.reference_audio.name == "ref.wav"
    assert spec.prompt.endswith("More cinematic") and spec.rewrite_caption is False
    assert [o["seed"] for o in job["outputs"]] == [40, 41]
    out = job["outputs"][0]
    assert out["filename"] == "00.mp3" and out["alt"]["wav"]["filename"] == "00.wav"
    assert out["loudness_lufs"] == pytest.approx(-14.0, abs=2.0)
    assert client.get(out["url"]).headers["content-type"] == "audio/mpeg"
    wav = client.get(out["alt"]["wav"]["url"])
    assert wav.status_code == 200 and len(wav.content) == out["alt"]["wav"]["bytes"]

    manifest = job["result"]
    assert manifest["params"]["caption"] == spec.prompt
    assert manifest["analysis"]["bpm"] == 81
    assert [v["seed"] for v in manifest["variations"]] == [40, 41]
    assert manifest["variations"][0]["runtime"]["dit_model"] == "acestep-v15-turbo"


def test_manifest_is_written_next_to_outputs(client, tmp_path):
    from app import main as main_mod

    sample = _upload(client, make_tone(tmp_path / "t.wav", seconds=10.0))
    resp = client.post("/v1/audio/vibe/generate", json={
        "sample_id": sample["sample_id"], "caption": "warm synth pad", "variations": 1,
    })
    job = _await(client, resp.json()["job_id"])
    on_disk = json.loads((Path(main_mod.CONFIG.data_dir) / job["job_id"] / "manifest.json").read_text())
    assert on_disk["params"]["caption"] == "Warm synth pad"
    assert on_disk["variations"][0]["seed"] == job["outputs"][0]["seed"]


def test_groove_mode_sends_cover(client, tmp_path):
    sample = _upload(client, make_tone(tmp_path / "t.wav", seconds=15.0))
    resp = client.post("/v1/audio/vibe/generate", json={
        "sample_id": sample["sample_id"], "mode": "groove", "caption": "jazz trio",
        "cover_strength": 0.35, "variations": 1, "format": "wav",
    })
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    spec = client.music.calls[-1]
    assert spec.task_type == "cover" and spec.cover_strength == 0.35
    assert spec.src_audio.name == "src.wav" and spec.duration_s == pytest.approx(15.0, abs=0.1)
    # WAV je primární formát, takže vedle něj žádný další.
    assert job["outputs"][0]["alt"] == {}


def test_generate_without_caption_or_analysis_is_conflict(client, tmp_path):
    sample = _upload(client, make_tone(tmp_path / "t.wav", seconds=8.0))
    resp = client.post("/v1/audio/vibe/generate", json={"sample_id": sample["sample_id"]})
    assert resp.status_code == 409


def test_unknown_sample_is_404(client):
    assert client.post("/v1/audio/vibe/analyze", json={"sample_id": "0" * 24}).status_code == 404
    assert client.post("/v1/audio/vibe/generate", json={"sample_id": "0" * 24}).status_code == 404
    assert client.get("/v1/audio/vibe/samples/" + "0" * 24).status_code == 404


def test_bad_upload_is_422(client):
    resp = client.post("/v1/audio/vibe/samples", files={"sample": ("x.mp3", b"nope" * 100, "audio/mpeg")})
    assert resp.status_code == 422


# --- adaptér ACE-Step ---


def test_acestep_uploads_reference_as_multipart(tmp_path, monkeypatch):
    import httpx

    from tests.test_backends import AUDIO_URL, WAV, make_acestep, query_result

    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)
    ref = make_tone(tmp_path / "ref.wav", seconds=1.0)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            seen["ctype"] = request.headers["content-type"]
            seen["body"] = request.content
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        if request.url.path == "/query_result":
            return httpx.Response(200, json=query_result(1, files=[AUDIO_URL]))
        return httpx.Response(200, content=WAV)

    make_acestep(handler).generate(GenSpec(
        prompt="warm lo-fi", duration_s=30, seed=3, reference_audio=ref,
        time_signature="4", rewrite_caption=False,
    ), tmp_path)
    assert seen["ctype"].startswith("multipart/form-data")
    body = seen["body"]
    assert b'name="reference_audio"; filename="ref.wav"' in body
    assert b'name="use_cot_caption"\r\n\r\nfalse' in body
    assert b'name="time_signature"\r\n\r\n4' in body
    assert b'name="task_type"' not in body  # text2music je výchozí
    assert b'name="thinking"\r\n\r\ntrue' in body  # výchozí GenSpec = jako /music


def test_acestep_cover_sends_strength(tmp_path, monkeypatch):
    import httpx

    from tests.test_backends import AUDIO_URL, WAV, make_acestep, query_result

    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)
    src = make_tone(tmp_path / "src.wav", seconds=1.0)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            seen["body"] = request.content
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        if request.url.path == "/query_result":
            return httpx.Response(200, json=query_result(1, files=[AUDIO_URL]))
        return httpx.Response(200, content=WAV)

    make_acestep(handler).generate(GenSpec(
        prompt="x", duration_s=20, task_type="cover", src_audio=src, cover_strength=0.3,
    ), tmp_path)
    assert b'name="src_audio"; filename="src.wav"' in seen["body"]
    assert b'name="task_type"\r\n\r\ncover' in seen["body"]
    assert b'name="audio_cover_strength"\r\n\r\n0.3' in seen["body"]


def test_acestep_analyze_returns_metadata_without_codes(tmp_path, monkeypatch):
    import httpx

    from tests.test_backends import make_acestep

    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)
    audio = make_tone(tmp_path / "src.wav", seconds=1.0)
    seen: dict = {}
    entry = {**LM_LOFI, "audio_codes": "<|audio_code_1|>" * 100, "audio_paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            seen["body"] = request.content
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        return httpx.Response(200, json={"code": 200, "data": [
            {"task_id": "t", "status": 1, "result": json.dumps([entry])}
        ]})

    result = make_acestep(handler).analyze(audio)
    assert b'name="full_analysis_only"\r\n\r\ntrue' in seen["body"]
    assert result["bpm"] == 81 and result["prompt"].startswith("A classic")
    assert "audio_codes" not in result


# --- úložiště ---


def test_existing_database_is_migrated(tmp_path):
    """jobs.db na SPARKu vznikla bez sloupců task/result a nesmí se ztratit."""
    from app.store import Store

    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '', request TEXT NOT NULL, outputs TEXT NOT NULL DEFAULT '[]',
            error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, started_at REAL, finished_at REAL);
        INSERT INTO jobs (job_id, kind, status, request, created_at) VALUES ('old', 'sfx', 'done', '{}', 1);
    """)
    con.commit()
    con.close()

    store = Store(str(db))
    old = store.get("old")
    assert old["task"] == "generate" and old["result"] is None
    job_id = store.create("music", {}, "m", task="analyze")
    store.mark_done(job_id, [], result={"bpm": 90})
    assert store.get(job_id)["result"] == {"bpm": 90}
    store.close()


# --- model neběží (SPARK mimo denní režim) ---


def test_vibe_refuses_early_when_model_is_down(client, tmp_path):
    """Upload jde i bez modelu, analýza a skládání dostanou hned 503 s důvodem."""
    from app.backends.base import MODEL_DOWN

    sample = _upload(client, make_tone(tmp_path / "t.wav", seconds=8.0))
    client.music.health = lambda: (False, "nedostupný: [Errno -3]")
    for path, body in (
        ("/v1/audio/vibe/analyze", {"sample_id": sample["sample_id"]}),
        ("/v1/audio/vibe/generate", {"sample_id": sample["sample_id"], "caption": "x"}),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 503
        assert resp.json()["detail"] == MODEL_DOWN


def test_model_going_down_mid_job_is_one_readable_error(client, tmp_path):
    from app.backends.base import MODEL_DOWN, BackendUnavailable

    sample = _upload(client, make_tone(tmp_path / "t.wav", seconds=8.0))
    calls = []

    def down(spec, workdir):
        calls.append(spec)
        raise BackendUnavailable("[Errno -3] Temporary failure in name resolution")

    client.music.generate = down
    resp = client.post("/v1/audio/vibe/generate", json={
        "sample_id": sample["sample_id"], "caption": "x", "variations": 3,
    })
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "error"
    assert job["error"] == MODEL_DOWN
    assert len(calls) == 1  # další varianty už se nezkoušely


def test_analysis_without_lm_is_not_cached(client, tmp_path):
    """Degradovaná analýza (LM odpověděl chybou) se k předloze neuloží —
    jinak by ji upload téhož souboru vracel pořád a nový poslech by nebyl."""
    sample = _upload(client, make_clicks(tmp_path / "c.wav", bpm=100, seconds=12))

    def broken(_):
        raise RuntimeError("LLM Understanding failed")

    client.music.analyze = broken
    resp = client.post("/v1/audio/vibe/analyze", json={"sample_id": sample["sample_id"]})
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["caption"] == "" and job["result"]["bpm"] is not None
    assert _upload(client, tmp_path / "c.wav")["analysis"] is None


def test_unreachable_lm_fails_analysis_instead_of_degrading(client, tmp_path):
    from app.backends.base import MODEL_DOWN, BackendUnavailable

    sample = _upload(client, make_clicks(tmp_path / "c.wav", bpm=100, seconds=12))

    def gone(_):
        raise BackendUnavailable("connection refused")

    client.music.analyze = gone
    resp = client.post("/v1/audio/vibe/analyze", json={"sample_id": sample["sample_id"]})
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "error" and job["error"] == MODEL_DOWN


def test_acestep_connect_error_is_unavailable(tmp_path):
    import httpx

    from app.backends.base import BackendUnavailable
    from tests.test_backends import make_acestep

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")

    with pytest.raises(BackendUnavailable):
        make_acestep(handler).generate(GenSpec(prompt="x", duration_s=10), tmp_path)
    with pytest.raises(BackendUnavailable):
        make_acestep(handler).analyze(make_tone(tmp_path / "a.wav", seconds=1.0))
