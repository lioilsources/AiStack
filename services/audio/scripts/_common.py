"""Sdílené kousky smoke/bench skriptů: klient services/audio + prompty."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
import json
from typing import Any

AUDIO_URL = os.environ.get("AUDIO_URL", "http://127.0.0.1:8093").rstrip("/")
API_KEY = os.environ.get("AUDIO_API_KEY", "")


class AudioError(RuntimeError):
    pass


def _request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 60.0):
    url = f"{AUDIO_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return json.loads(body) if "json" in ctype else body
    except urllib.error.HTTPError as exc:
        raise AudioError(f"{method} {path} → HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise AudioError(f"{method} {path} → {exc.reason}") from exc


def submit(kind: str, payload: dict[str, Any]) -> str:
    return _request("POST", f"/v1/audio/{kind}", payload)["job_id"]


def wait(job_id: str, timeout_s: float = 1200.0, poll_s: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = _request("GET", f"/v1/audio/jobs/{job_id}")
        if job["status"] == "done":
            return job
        if job["status"] == "error":
            raise AudioError(f"job {job_id} selhal: {job['error']}")
        time.sleep(poll_s)
    raise AudioError(f"job {job_id} nedoběhl do {timeout_s:.0f}s")


def download(output: dict[str, Any], dest: str) -> int:
    data = _request("GET", output["url"], timeout=300.0)
    with open(dest, "wb") as fh:
        fh.write(data)
    return len(data)


# Prompty odpovídají tomu, co Kirian opravdu potřebuje — ne obecné „upbeat pop".
MUSIC_PROMPTS = [
    ("chiptune_boss", "chiptune, 8-bit, NES square wave lead, boss battle, driving, minor key, [instrumental]", 140),
    ("arcade_synthwave", "synthwave, 1990s arcade, analog arpeggios, four-on-the-floor drums, neon, [instrumental]", 128),
    ("snes_overworld", "16-bit SNES, FM synthesis, sampled orchestra hits, heroic overworld theme, [instrumental]", 132),
]

SFX_PROMPTS = [
    ("fire_bullet", "short 8-bit laser blip, single shot, dry", 0.5),
    ("fire_beam", "sustained energy beam hum, steady, focused", 0.8),
    ("hit_shield", "energy shield deflection, electronic ping, short", 0.5),
    ("hit_hull", "metallic hull impact, whip-crack hit, dry", 0.5),
    ("explosion_small", "small explosion, sharp bang, instant attack, short tail", 0.5),
    ("explosion_large", "big explosion, hard sharp bang, punchy body, short tail", 1.2),
    ("pickup", "item pickup, ascending chime, clean and short", 0.5),
    ("weapon_unlock", "power-up unlock, triumphant rising fanfare", 1.5),
    ("sector_complete", "level complete, uplifting victory fanfare", 2.0),
    ("game_over", "defeat sting, descending somber tone", 2.5),
]
