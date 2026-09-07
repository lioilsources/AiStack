"""API kontrakt proti falešnému backendu — bez GPU a bez modelů."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.backends.base import BackendError, GenSpec, RawAudio
from tests.conftest import make_tone


class FakeBackend:
    def __init__(self, kind: str, *, fail: bool = False) -> None:
        self.kind = kind
        self.name = f"fake-{kind}"
        self.fail = fail
        self.calls: list[GenSpec] = []
        self.unloaded: list[str] = []

    def generate(self, spec: GenSpec, workdir: Path) -> RawAudio:
        self.calls.append(spec)
        if self.fail:
            raise BackendError("model spadl")
        path = make_tone(workdir / "raw.wav", seconds=max(1.0, spec.duration_s))
        return RawAudio(path=path, seed=spec.seed, model=spec.model or self.name)

    def health(self) -> tuple[bool, str]:
        return True, "fake"

    def loaded_models(self) -> list[str]:
        return ["acestep-v15-turbo"] if self.kind == "music" else ["moss-soundeffect-v2"]

    def unload(self, name: str) -> None:
        self.unloaded.append(name)


@pytest.fixture
def client(monkeypatch):
    from app import main as main_mod

    music, sfx = FakeBackend("music"), FakeBackend("sfx")
    monkeypatch.setattr(main_mod, "AceStepBackend", lambda *a, **k: music)
    monkeypatch.setattr(main_mod, "SfxHTTPBackend", lambda *a, **k: sfx)
    with TestClient(main_mod.app) as c:
        c.fakes = {"music": music, "sfx": sfx}  # type: ignore[attr-defined]
        yield c


def _await(client, job_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/audio/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} nedoběhl: {job}")


def test_health_reports_idle_time_per_kind(client):
    """Controller potřebuje vědět, jak dlouho se negeneruje, než shodí model."""
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert sorted(payload["backends"]) == ["music", "sfx"]
    assert set(payload["idle_s"]) == {"music", "sfx"}
    # Před prvním jobem None, ne Infinity — to by nebyl platný JSON.
    assert payload["idle_s"]["music"] is None
    assert "Infinity" not in client.get("/health").text


def test_models_carry_license(client):
    models = client.get("/v1/audio/models").json()
    names = {m["name"] for m in models}
    assert "acestep-v15-turbo" in names
    assert "moss-soundeffect-v2" in names
    # Nekomerční model se do katalogu nesmí dostat — hra jde na Steam.
    assert all(m["commercial"] for m in models)
    acestep = next(m for m in models if m["name"] == "acestep-v15-turbo")
    assert acestep["license"] == "MIT"
    assert acestep["loaded"] is True


def test_unserved_model_is_not_reported_available(client):
    """Stažený model bez runtime nesmí vypadat dostupně — job by spadl až v generaci."""
    models = {m["name"]: m for m in client.get("/v1/audio/models").json()}
    assert models["acestep-v15-turbo"]["available"] is True
    assert models["ace-step-v1-3.5b"]["available"] is False
    assert models["ace-step-v1-3.5b"]["commercial"] is True  # licence je v pořádku
    assert "runtime" in models["ace-step-v1-3.5b"]["detail"]


def test_excluded_models_documented(client):
    excluded = client.get("/v1/audio/models/excluded").json()
    assert any("musicgen" in k for k in excluded)
    assert "MMAudio" in " ".join(excluded)


def test_music_job_end_to_end(client):
    resp = client.post("/v1/audio/music", json={
        "prompt": "chiptune boss theme", "duration_s": 8, "bpm": 140, "seed": 7, "loop": True,
    })
    assert resp.status_code == 202
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]

    out = job["outputs"][0]
    assert out["seed"] == 7
    assert out["duration"] == pytest.approx(6.0, abs=0.5)  # 8 s minus 2 s prolnutí
    assert out["loudness_lufs"] == pytest.approx(-16.0, abs=2.0)
    assert len(out["sha256"]) == 64

    audio = client.get(out["url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/ogg"
    assert len(audio.content) == out["bytes"]


def test_sfx_variations_get_distinct_seeds(client):
    resp = client.post("/v1/audio/sfx", json={
        "prompt": "laser blip", "duration_s": 1, "variations": 3, "seed": 100,
    })
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    seeds = [o["seed"] for o in job["outputs"]]
    assert seeds == [100, 101, 102]
    assert len({o["filename"] for o in job["outputs"]}) == 3


def test_seed_is_recorded_even_when_caller_omits_it(client):
    """Bez seedu v manifestu nejde asset deterministicky zregenerovat."""
    resp = client.post("/v1/audio/sfx", json={"prompt": "explosion", "duration_s": 1})
    job = _await(client, resp.json()["job_id"])
    assert isinstance(job["outputs"][0]["seed"], int)


def test_failing_backend_marks_job_error(client, monkeypatch):
    client.fakes["sfx"].fail = True
    resp = client.post("/v1/audio/sfx", json={"prompt": "boom", "duration_s": 1})
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "error"
    assert "model spadl" in job["error"]


def test_unknown_job_is_404(client):
    assert client.get("/v1/audio/jobs/neexistuje").status_code == 404


def test_output_path_traversal_is_rejected(client):
    resp = client.post("/v1/audio/sfx", json={"prompt": "ping", "duration_s": 1})
    job_id = resp.json()["job_id"]
    _await(client, job_id)
    assert client.get(f"/v1/audio/jobs/{job_id}/outputs/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_elevenlabs_sfx_shim_returns_audio(client):
    resp = client.post("/v1/sound-generation", json={
        "text": "short 8-bit laser blip", "duration_seconds": 1.0, "prompt_influence": 0.5,
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/ogg"
    assert len(resp.content) > 0
    assert client.fakes["sfx"].calls[-1].prompt == "short 8-bit laser blip"


def test_elevenlabs_music_shim_maps_length(client):
    resp = client.post("/v1/music/compose", json={
        "prompt": "arcade synthwave loop", "music_length_ms": 8000,
    })
    assert resp.status_code == 200
    assert client.fakes["music"].calls[-1].duration_s == pytest.approx(8.0)


def test_shim_rejects_empty_prompt(client):
    assert client.post("/v1/sound-generation", json={"text": "  "}).status_code == 422


def test_model_unload_routes_to_backend(client):
    assert client.post("/v1/audio/models/moss-soundeffect-v2/unload").status_code == 200
    assert client.fakes["sfx"].unloaded == ["moss-soundeffect-v2"]


def test_unload_unknown_model_is_404(client):
    assert client.post("/v1/audio/models/neexistuje/unload").status_code == 404


def test_api_key_is_enforced_when_configured(monkeypatch, tmp_path):
    """Prázdný klíč = jen interní síť; nastavený klíč musí opravdu platit."""
    from fastapi.testclient import TestClient

    from app import main as main_mod

    monkeypatch.setenv("AUDIO_API_KEY", "tajne")
    monkeypatch.setattr(main_mod, "AceStepBackend", lambda *a, **k: FakeBackend("music"))
    monkeypatch.setattr(main_mod, "SfxHTTPBackend", lambda *a, **k: FakeBackend("sfx"))

    with TestClient(main_mod.app) as c:
        assert c.get("/health").status_code == 200  # health je bez klíče
        assert c.get("/v1/audio/models").status_code == 401
        assert c.get("/v1/audio/models", headers={"Authorization": "Bearer tajne"}).status_code == 200
        # ElevenLabs klienti posílají klíč vlastní hlavičkou.
        assert c.get("/v1/audio/models", headers={"xi-api-key": "tajne"}).status_code == 200
        assert c.get("/v1/audio/models", headers={"xi-api-key": "spatne"}).status_code == 401
