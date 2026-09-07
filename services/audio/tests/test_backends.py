"""Adaptéry na modelové kontejnery — proti falešnému HTTP, bez GPU.

ACE-Step balí odpovědi do obálky `{code, data}`, stav se zjišťuje POSTem na
/query_result a audio se stahuje třetím requestem. Každý z těch tří kroků má
vlastní způsob, jak selhat, a všechny se projeví až v produkci, když je
netestuju tady.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.backends.acestep import AceStepBackend
from app.backends.base import BackendError, GenSpec
from app.backends.sfxhttp import SfxHTTPBackend


def make_acestep(handler, timeout_s: float = 5.0) -> AceStepBackend:
    backend = AceStepBackend("http://audio-music:8001", "acestep-v15-turbo", timeout_s)
    backend._client = httpx.Client(
        base_url="http://audio-music:8001", transport=httpx.MockTransport(handler)
    )
    return backend


def make_sfx(handler, timeout_s: float = 5.0) -> SfxHTTPBackend:
    backend = SfxHTTPBackend("http://audio-sfx:8002", "moss-soundeffect-v2", timeout_s)
    backend._client = httpx.Client(
        base_url="http://audio-sfx:8002", transport=httpx.MockTransport(handler)
    )
    return backend


WAV = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32
AUDIO_URL = "/v1/audio?path=%2Ftmp%2Fout%2Ftrack.wav"


def query_result(status: int, *, files: list[str] | None = None, error: str = "") -> dict:
    """Odpověď ve tvaru, který ACE-Step opravdu vrací.

    `result` je JSON **řetězec** se seznamem položek, `status` je číslo
    (0 běží, 1 hotovo, 2 chyba) a cesta k audiu je v položce pod `file`.
    """
    entries = [{"file": f, "status": status, "metas": {}} for f in (files or [])]
    if not entries:
        entries = [{"file": "", "status": status, "error": error or None}]
    return {
        "code": 200,
        "data": [{"task_id": "t1", "result": json.dumps(entries), "status": status,
                  "progress_text": "diffusing"}],
    }


def test_music_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)
    sent: dict = {}
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t1", "status": "queued"}})
        if request.url.path == "/query_result":
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(200, json=query_result(0))
            return httpx.Response(200, json=query_result(1, files=[AUDIO_URL]))
        if request.url.path == "/v1/audio":
            assert request.url.params["path"] == "/tmp/out/track.wav"
            return httpx.Response(200, content=WAV)
        raise AssertionError(f"neočekávaná cesta {request.url.path}")

    backend = make_acestep(handler)
    raw = backend.generate(GenSpec(
        prompt="chiptune boss theme", duration_s=40, seed=7, bpm=140, key="C major",
    ), tmp_path)

    assert raw.path.read_bytes() == WAV
    # /query_result seed nevrací, takže v manifestu skončí ten zadaný — proto
    # ho jobs.Runner dopočítá i tehdy, když ho volající nezadal.
    assert raw.seed == 7
    assert sent["audio_duration"] == 40
    assert sent["bpm"] == 140 and sent["key_scale"] == "C major"
    assert sent["use_random_seed"] is False and sent["seed"] == 7
    assert sent["lyrics"] == ""  # instrumental=True


def test_music_without_seed_asks_for_random(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        if request.url.path == "/query_result":
            return httpx.Response(200, json=query_result(1, files=[AUDIO_URL]))
        return httpx.Response(200, content=WAV)

    make_acestep(handler).generate(GenSpec(prompt="x", duration_s=10), tmp_path)
    assert sent["use_random_seed"] is True
    assert "seed" not in sent


def test_lyrics_pass_through_when_not_instrumental(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        if request.url.path == "/query_result":
            return httpx.Response(200, json=query_result(1, files=[AUDIO_URL]))
        return httpx.Response(200, content=WAV)

    make_acestep(handler).generate(
        GenSpec(prompt="x", duration_s=10, instrumental=False, lyrics="la la la"), tmp_path
    )
    assert sent["lyrics"] == "la la la"


def test_failed_job_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        return httpx.Response(200, json=query_result(2, error="CUDA out of memory"))

    with pytest.raises(BackendError, match="CUDA out of memory"):
        make_acestep(handler).generate(GenSpec(prompt="x", duration_s=10), tmp_path)


def test_full_queue_raises(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Server busy")

    with pytest.raises(BackendError, match="fronta"):
        make_acestep(handler).generate(GenSpec(prompt="x", duration_s=10), tmp_path)


def test_timeout_reports_last_status(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        return httpx.Response(200, json=query_result(0))

    with pytest.raises(BackendError, match="diffusing"):
        make_acestep(handler, timeout_s=0.05).generate(GenSpec(prompt="x", duration_s=10), tmp_path)


def test_download_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        if request.url.path == "/query_result":
            return httpx.Response(200, json=query_result(1, files=[AUDIO_URL]))
        return httpx.Response(403, text="Access denied")

    with pytest.raises(BackendError, match="403"):
        make_acestep(handler).generate(GenSpec(prompt="x", duration_s=10), tmp_path)


def test_job_without_audio_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backends.acestep._POLL_INTERVAL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            return httpx.Response(200, json={"data": {"task_id": "t"}})
        return httpx.Response(200, json=query_result(1))

    with pytest.raises(BackendError, match="bez audia"):
        make_acestep(handler).generate(GenSpec(prompt="x", duration_s=10), tmp_path)


def test_unwrap_handles_envelope_variants():
    """Obálka se napříč verzemi ACE-Step liší — poznávat ji podle `code` nestačí."""
    from app.backends.acestep import _first_item, _unwrap

    assert _first_item(_unwrap({"code": 200, "data": [{"status": 0}]})) == {"status": 0}
    assert _unwrap({"code": 0, "data": {"task_id": "t"}}) == {"task_id": "t"}
    assert _unwrap({"message": "ok", "data": {"task_id": "t"}}) == {"task_id": "t"}
    assert _unwrap({"data": {"task_id": "t"}}) == {"task_id": "t"}
    assert _unwrap({"task_id": "t"}) == {"task_id": "t"}
    # Výsledek, který sám nese `data`, se nesmí rozbalit podruhé.
    assert _unwrap({"status": "done", "data": "něco"})["status"] == "done"



def test_health_reports_unreachable():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    available, detail = make_acestep(handler).health()
    assert available is False
    assert "nedostupný" in detail


# --- audio-sfx ---


def test_sfx_generate_returns_seed_from_header(tmp_path):
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/generate"
        sent.update(json.loads(request.content))
        return httpx.Response(200, content=WAV, headers={"X-Seed": "99", "X-Model": "moss-soundeffect-v2"})

    raw = make_sfx(handler).generate(GenSpec(prompt="laser", duration_s=0.5, seed=99), tmp_path)
    assert raw.seed == 99
    assert raw.model == "moss-soundeffect-v2"
    assert sent["duration_s"] == 0.5
    assert raw.path.read_bytes() == WAV


def test_sfx_empty_response_raises(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with pytest.raises(BackendError, match="prázdné audio"):
        make_sfx(handler).generate(GenSpec(prompt="x", duration_s=1), tmp_path)


def test_sfx_error_status_raises(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="CUDA out of memory")

    with pytest.raises(BackendError, match="CUDA out of memory"):
        make_sfx(handler).generate(GenSpec(prompt="x", duration_s=1), tmp_path)


def test_sfx_unload_calls_runtime():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    make_sfx(handler).unload("moss-soundeffect-v2")
    assert calls == ["/models/moss-soundeffect-v2/unload"]


def test_result_entries_parses_json_string():
    """`result` chodí jako JSON řetězec — hotový seznam i dict musí projít taky."""
    from app.backends.acestep import _files, _result_entries, _status_code

    item = {"result": json.dumps([{"file": "/v1/audio?path=a.wav"}]), "status": 1}
    assert _result_entries(item) == [{"file": "/v1/audio?path=a.wav"}]
    assert _files(_result_entries(item)) == ["/v1/audio?path=a.wav"]
    assert _status_code(item) == 1
    assert _status_code({"status": "succeeded"}) == 1
    assert _status_code({"status": 0}) == 0
    assert _result_entries({"result": "nevalidní json"}) == []
    assert _result_entries({"result": [{"file": "x"}]}) == [{"file": "x"}]
