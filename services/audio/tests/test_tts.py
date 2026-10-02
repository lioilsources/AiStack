"""TTS — výběr enginu a licence, API, uložené hlasy, adaptér. Bez modelů a GPU."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backends.base import BackendError, BackendUnavailable, RawAudio
from app.backends.ttshttp import TtsHTTPBackend, TtsSpec
from app.tts.resolve import ResolveError, resolve
from app.tts.voicestore import VoiceError, VoiceStore
from tests.conftest import make_tone
from tests.test_api import FakeBackend


def speech_like(path: Path, seconds: float) -> bytes:
    """Tón s tichem na krajích — ingest ořezává ticho na začátku."""
    make_tone(path, seconds=seconds, silence_pad=0.5)
    return path.read_bytes()


@pytest.fixture
def store(tmp_path) -> VoiceStore:
    voices = VoiceStore(tmp_path / "voices")
    voices.ingest("smug-cat", speech_like(tmp_path / "ref.wav", 6.0), rights="own", source="já, mikrofon 2026-10")
    return voices


# --- resolve: pravidla výběru ---


def test_english_defaults_to_kokoro():
    plan = resolve(language="en-US")
    assert (plan.engine, plan.model.name, plan.voice) == ("kokoro", "kokoro-82m", "af_heart")
    assert plan.engine_language == "en-us"
    assert plan.lane == "tts-cpu"


def test_british_voice_gets_british_espeak():
    plan = resolve(language="en", voice="bm_george")
    assert plan.engine_language == "en-gb"


def test_czech_defaults_to_commercial_piper_voice():
    plan = resolve(language="cs")
    assert plan.model.name == "piper-cs-kasandra-medium"
    assert plan.model.commercial is True
    assert "Šimek" in plan.model.attribution


def test_xtts_is_refused_for_commercial_use():
    with pytest.raises(ResolveError) as exc:
        resolve(language="cs", engine="xtts")
    assert exc.value.status == 403
    assert "commercial_only=false" in exc.value.detail


def test_xtts_allowed_when_caller_opts_out():
    plan = resolve(language="cs", engine="xtts", commercial_only=False)
    assert plan.model.name == "xtts-v2"
    assert plan.voice_kind == "builtin"
    assert plan.lane == "tts-gpu"


def test_jirka_is_unclear_and_refused():
    with pytest.raises(ResolveError) as exc:
        resolve(language="cs", voice="cs_CZ-jirka-medium")
    assert exc.value.status == 403
    plan = resolve(language="cs", voice="piper:cs_CZ-jirka-medium", commercial_only=False)
    assert plan.model.name == "piper-cs-jirka-medium"


def test_kokoro_cannot_speak_czech():
    with pytest.raises(ResolveError) as exc:
        resolve(language="cs", engine="kokoro")
    assert exc.value.status == 422
    assert "kasandra" in exc.value.detail


def test_chatterbox_czech_goes_to_czech_t3_and_needs_opt_out():
    with pytest.raises(ResolveError):
        resolve(language="cs", engine="chatterbox")
    plan = resolve(language="cs", engine="chatterbox", commercial_only=False)
    assert plan.model.name == "chatterbox-cs"


def test_voice_language_mismatch_is_rejected():
    with pytest.raises(ResolveError) as exc:
        resolve(language="es", voice="af_heart")
    assert exc.value.status == 422


def test_unknown_voice_is_404():
    with pytest.raises(ResolveError) as exc:
        resolve(language="en", voice="nikdo")
    assert exc.value.status == 404


def test_custom_voice_english_clones_with_chatterbox(store):
    plan = resolve(language="en", voice="smug-cat", voices=store)
    assert plan.model.name == "chatterbox-multilingual"
    assert plan.voice_kind == "custom"
    assert plan.ref_path is not None and plan.ref_path.is_file()


def test_custom_voice_czech_has_no_commercial_cloner(store):
    with pytest.raises(ResolveError) as exc:
        resolve(language="cs", voice="custom:smug-cat", voices=store)
    assert exc.value.status == 403
    plan = resolve(language="cs", voice="smug-cat", voices=store, commercial_only=False)
    assert plan.model.name == "chatterbox-cs"
    plan = resolve(language="cs", voice="smug-cat", engine="xtts", voices=store, commercial_only=False)
    assert plan.model.name == "xtts-v2"


def test_cpu_engine_cannot_clone(store):
    with pytest.raises(ResolveError) as exc:
        resolve(language="en", voice="smug-cat", engine="kokoro", voices=store)
    assert exc.value.status == 422


def test_missing_custom_voice_is_404(store):
    with pytest.raises(ResolveError) as exc:
        resolve(language="en", voice="custom:nikdo", voices=store)
    assert exc.value.status == 404


# --- uložené hlasy ---


def test_ingest_normalizes_and_caps_length(tmp_path):
    voices = VoiceStore(tmp_path / "v")
    voice = voices.ingest("long", speech_like(tmp_path / "l.wav", 40.0), rights="synthetic", source="kokoro af_heart")
    assert voice.duration_s == pytest.approx(30.0, abs=0.2)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "json",
         str(voices.ref_path("long"))],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (int(stream["sample_rate"]), stream["channels"]) == (24000, 1)


def test_ingest_requires_rights_and_source(tmp_path):
    voices = VoiceStore(tmp_path / "v")
    data = speech_like(tmp_path / "a.wav", 5.0)
    with pytest.raises(VoiceError, match="rights"):
        voices.ingest("aa", data, rights="found-on-youtube", source="youtube")
    with pytest.raises(VoiceError, match="source"):
        voices.ingest("aa", data, rights="own", source="")


def test_ingest_rejects_too_short_and_bad_ids(tmp_path):
    voices = VoiceStore(tmp_path / "v")
    with pytest.raises(VoiceError, match="aspoň"):
        voices.ingest("short", speech_like(tmp_path / "s.wav", 1.0), rights="own", source="já sám")
    with pytest.raises(VoiceError, match="neplatné id"):
        voices.ingest("../etc", b"x", rights="own", source="já sám")


def test_ingest_does_not_overwrite_without_replace(store, tmp_path):
    data = speech_like(tmp_path / "b.wav", 5.0)
    with pytest.raises(VoiceError, match="už existuje"):
        store.ingest("smug-cat", data, rights="own", source="já sám")
    assert store.ingest("smug-cat", data, rights="own", source="já sám", replace=True).duration_s < 6


# --- adaptér ---


def make_tts(handler) -> TtsHTTPBackend:
    backend = TtsHTTPBackend(
        {"kokoro": "http://audio-tts:8003", "piper": "http://audio-tts:8003",
         "xtts": "http://audio-tts-xtts:8004", "chatterbox": "http://audio-tts-chatterbox:8005"},
        timeout_s=5.0,
    )
    for engine, url in backend.urls.items():
        backend._clients[url] = httpx.Client(base_url=url, transport=httpx.MockTransport(handler))
    return backend


WAV = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 64


def test_adapter_sends_reference_for_custom_voice(tmp_path, store):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, content=WAV, headers={"X-Seed": "7", "X-Model": "chatterbox-multilingual",
                                                         "X-Watermark": "resemble-perth"})

    backend = make_tts(handler)
    spec = TtsSpec(text="hi", language="en", engine="chatterbox", model="chatterbox-multilingual",
                   voice="smug-cat", voice_kind="custom", ref_path=store.ref_path("smug-cat"), seed=7)
    raw = backend.generate(spec, tmp_path)
    assert seen["url"].startswith("http://audio-tts-chatterbox:8005/synthesize")
    assert b'name="reference"' in seen["body"]
    assert b'"voice": ""' in seen["body"]  # klon nemá jméno presetu
    assert raw.seed == 7 and raw.info["watermark"] == "resemble-perth"


def test_adapter_preset_has_no_reference(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, content=WAV)

    backend = make_tts(handler)
    backend.generate(TtsSpec(text="ahoj", language="cs", engine="piper", model="piper-cs-kasandra-medium",
                             voice="cs_CZ-kasandra-medium"), tmp_path)
    assert b'name="reference"' not in seen["body"]
    assert "ahoj" in seen["body"].decode()


def test_adapter_connect_error_is_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    backend = make_tts(handler)
    with pytest.raises(BackendUnavailable) as exc:
        backend.generate(TtsSpec(text="hi", language="en", engine="xtts", model="xtts-v2", voice="Ana Florence"),
                         tmp_path)
    assert "audio-tts-xtts" in str(exc.value)


def test_adapter_http_error_is_backend_error(tmp_path):
    backend = make_tts(lambda r: httpx.Response(502, text="CUDA out of memory"))
    with pytest.raises(BackendError, match="out of memory"):
        backend.generate(TtsSpec(text="hi", language="en", engine="kokoro", model="kokoro-82m", voice="af_heart"),
                         tmp_path)


# --- API proti falešnému TTS backendu ---


class FakeTts:
    kind = "tts"
    name = "fake-tts"

    def __init__(self) -> None:
        self.urls = {"kokoro": "x", "piper": "x", "xtts": "y", "chatterbox": "z"}
        self.down: set[str] = set()
        self.calls: list[TtsSpec] = []

    def engine_health(self, engine: str) -> tuple[bool, str]:
        return (engine not in self.down), "fake"

    def health(self) -> tuple[bool, str]:
        return True, "fake"

    def engine_models(self, engine: str) -> dict:
        avail = {"kokoro": ["kokoro-82m"], "piper": ["piper-cs-kasandra-medium"],
                 "xtts": ["xtts-v2"], "chatterbox": ["chatterbox-multilingual"]}[engine]
        return {"available": avail, "loaded": avail[:1]}

    def loaded_models(self) -> list[str]:
        return ["kokoro-82m"]

    def engine_voices(self, engine: str) -> list[dict]:
        return [{"id": "Ana Florence", "model": "xtts-v2"}] if engine == "xtts" else [
            {"id": "default", "model": "chatterbox-multilingual"}]

    def generate(self, spec: TtsSpec, workdir: Path) -> RawAudio:
        self.calls.append(spec)
        stereo = make_tone(workdir / "stereo.wav", seconds=2.0, silence_pad=0.3)
        # Skutečné TTS modely vrací mono — stereo by po downmixu vyšlo o 3 dB
        # tišší než cíl (normalizace měří před -ac 1).
        path = workdir / "raw.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(stereo), "-ac", "1", str(path)], check=True)
        return RawAudio(path=path, seed=None, model=spec.model, info={"engine": spec.engine})


@pytest.fixture
def client(monkeypatch):
    from app import main as main_mod

    tts = FakeTts()
    monkeypatch.setattr(main_mod, "AceStepBackend", lambda *a, **k: FakeBackend("music"))
    monkeypatch.setattr(main_mod, "SfxHTTPBackend", lambda *a, **k: FakeBackend("sfx"))
    monkeypatch.setattr(main_mod, "TtsHTTPBackend", lambda *a, **k: tts)
    with TestClient(main_mod.app) as c:
        c.tts = tts  # type: ignore[attr-defined]
        yield c


def _await(client, job_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/audio/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} nedoběhl")


def test_models_list_tts_with_licenses(client):
    models = {m["name"]: m for m in client.get("/v1/audio/models?kind=tts").json()}
    assert models["xtts-v2"]["commercial"] is False
    assert models["xtts-v2"]["license_status"] == "noncommercial"
    assert "cs" in models["xtts-v2"]["languages"]
    assert models["chatterbox-multilingual"]["watermark"] is True
    assert "cs" not in models["chatterbox-multilingual"]["languages"]
    assert models["piper-cs-kasandra-medium"]["attribution"]
    # chatterbox-cs runtime nehlásí (váhy nestažené) → nedostupný, ale v katalogu je.
    assert models["chatterbox-cs"]["available"] is False
    assert "váhy" in models["chatterbox-cs"]["detail"]


def test_commercial_filter_drops_xtts(client):
    names = {m["name"] for m in client.get("/v1/audio/models?commercial=true").json()}
    assert "xtts-v2" not in names and "kokoro-82m" in names and "acestep-v15-turbo" in names


def test_tts_job_end_to_end_records_license(client):
    resp = client.post("/v1/audio/tts", json={"text": "Ahoj, tady Kasandra.", "language": "cs"})
    assert resp.status_code == 202, resp.text
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["kind"] == "tts" and job["task"] == "tts"
    lic = job["result"]["license"]
    assert lic["model"] == "piper-cs-kasandra-medium" and lic["commercial"] is True
    assert "CC BY" in lic["attribution"]

    out = job["outputs"][0]
    assert out["loudness_lufs"] == pytest.approx(-16.0, abs=2.0)
    audio = client.get(out["url"])
    assert audio.headers["content-type"] == "audio/wav"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "json", "-"],
        input=audio.content, capture_output=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (int(stream["sample_rate"]), stream["channels"]) == (48000, 1)
    assert client.tts.calls[-1].voice == "cs_CZ-kasandra-medium"


def test_tts_xtts_refused_by_default(client):
    resp = client.post("/v1/audio/tts", json={"text": "hello", "engine": "xtts"})
    assert resp.status_code == 403
    resp = client.post("/v1/audio/tts", json={"text": "hello", "engine": "xtts", "commercial_only": False})
    assert resp.status_code == 202


def test_tts_engine_down_is_503_not_failed_job(client):
    client.tts.down.add("chatterbox")
    resp = client.post("/v1/audio/tts", json={"text": "hello", "engine": "chatterbox"})
    assert resp.status_code == 503
    assert "audio-tts-chatterbox" in resp.json()["detail"]


def test_voice_upload_list_and_clone(client, tmp_path):
    data = speech_like(tmp_path / "v.wav", 8.0)
    resp = client.post(
        "/v1/audio/voices",
        data={"voice_id": "boss", "rights": "consented", "source": "Pepa, svolení e-mailem 2026-10-01",
              "language": "en"},
        files={"sample": ("boss.wav", data, "audio/wav")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["duration_s"] > 7

    voices = client.get("/v1/audio/voices?type=custom&commercial=true").json()
    assert {(v["id"], v["model"]) for v in voices} == {("custom:boss", "chatterbox-multilingual")}
    assert client.get("/v1/audio/voices/boss/sample").status_code == 200

    resp = client.post("/v1/audio/tts", json={"text": "You fool.", "voice": "boss"})
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["result"]["voice"]["stored"]["rights"] == "consented"
    call = client.tts.calls[-1]
    assert call.voice_kind == "custom" and call.ref_path is not None

    assert client.delete("/v1/audio/voices/boss").status_code == 200
    assert client.get("/v1/audio/voices/boss").status_code == 404


def test_voice_upload_without_rights_is_422(client, tmp_path):
    resp = client.post(
        "/v1/audio/voices",
        data={"voice_id": "x1", "rights": "", "source": "?"},
        files={"sample": ("x.wav", speech_like(tmp_path / "x.wav", 5.0), "audio/wav")},
    )
    assert resp.status_code == 422


def test_voices_catalog_filters(client):
    cs = client.get("/v1/audio/voices?language=cs&commercial=true").json()
    assert [v["id"] for v in cs] == ["piper:cs_CZ-kasandra-medium"]
    xtts = client.get("/v1/audio/voices?engine=xtts").json()
    assert xtts and all(not v["commercial"] for v in xtts)
    en = client.get("/v1/audio/voices?language=en&engine=kokoro").json()
    assert any(v["id"] == "kokoro:af_heart" and v["grade"] == "A" for v in en)


def test_openai_speech_shim(client):
    resp = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "Hello there", "voice": "am_puck",
                                                 "response_format": "wav"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    assert client.tts.calls[-1].engine == "kokoro"
    # Český Piper hlas si jazyk odvodí sám.
    resp = client.post("/v1/audio/speech", json={"model": "piper", "input": "Ahoj",
                                                 "voice": "cs_CZ-kasandra-medium", "response_format": "wav"})
    assert resp.status_code == 200
    assert client.tts.calls[-1].language == "cs"


def test_health_reports_tts_lanes(client):
    idle = client.get("/health").json()["idle_s"]
    assert {"tts-cpu", "tts-gpu"} <= set(idle)
