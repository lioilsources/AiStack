#!/usr/bin/env python3
"""Smoke test TTS přes orchestrátor: EN (Kokoro), CZ (Piper), volitelně GPU enginy.

    python3 services/audio/scripts/smoke_tts.py            # jen CPU (Kokoro, Piper)
    python3 services/audio/scripts/smoke_tts.py --gpu      # + XTTS-v2 a Chatterbox
    python3 services/audio/scripts/smoke_tts.py --gpu --ref postava.wav   # + klonování

Výstupy jdou do bench/smoke_tts/ — poslechnout! Čísla neřeknou, jestli
čeština nezní cize.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import API_KEY, AUDIO_URL, AudioError, _request, download, wait  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / "bench" / "smoke_tts"

EN = "When you finally fix the bug at three in the morning, and the tests still fail."
CS = "Když konečně opravíš chybu ve tři ráno a testy pořád padají. Příliš žluťoučký kůň úpěl ďábelské ódy."

CPU_CASES = [
    ("kokoro_en_heart", {"text": EN, "language": "en", "voice": "af_heart"}),
    ("kokoro_en_puck", {"text": EN, "language": "en", "voice": "am_puck"}),
    ("kokoro_en_george", {"text": EN, "language": "en", "voice": "bm_george"}),
    ("piper_cs_kasandra", {"text": CS, "language": "cs"}),
    ("piper_cs_jirka", {"text": CS, "language": "cs", "voice": "cs_CZ-jirka-medium", "commercial_only": False}),
]

GPU_CASES = [
    ("xtts_en_builtin", {"text": EN, "language": "en", "engine": "xtts", "commercial_only": False, "seed": 42}),
    ("xtts_cs_builtin", {"text": CS, "language": "cs", "engine": "xtts", "commercial_only": False, "seed": 42}),
    ("chatterbox_en_default", {"text": EN, "language": "en", "engine": "chatterbox", "seed": 42}),
    # Jen když je stažený Thomcles/Chatterbox-TTS-Czech (gated).
    ("chatterbox_cs_default", {"text": CS, "language": "cs", "engine": "chatterbox", "commercial_only": False,
                               "seed": 42}),
]


def upload_voice(path: pathlib.Path, voice_id: str) -> None:
    boundary = uuid.uuid4().hex
    fields = {"voice_id": voice_id, "rights": "own", "source": f"smoke test, {path.name}", "replace": "true"}
    body = b""
    for key, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sample\"; filename=\"{path.name}\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{AUDIO_URL}/v1/audio/voices", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        print(f"  hlas {voice_id}: {json.loads(resp.read())['duration_s']} s reference")


def run(cases: list[tuple[str, dict]]) -> int:
    failures = 0
    for name, payload in cases:
        started = time.monotonic()
        try:
            job_id = _request("POST", "/v1/audio/tts", {**payload, "format": "wav"})["job_id"]
            job = wait(job_id, timeout_s=900, poll_s=0.5)
        except AudioError as exc:
            print(f"  {name:24s} SELHALO: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        out = job["outputs"][0]
        download(out, str(OUT / f"{name}.wav"))
        lic = job["result"]["license"]
        rtf = elapsed / out["duration"] if out["duration"] else 0
        print(f"  {name:24s} {elapsed:6.1f}s  {out['duration']:5.2f}s řeči  RTF {rtf:4.2f}  "
              f"{out['loudness_lufs']:6.1f} LUFS  {lic['model']} ({'komerčně' if lic['commercial'] else 'NEKOMERČNÍ'})")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", action="store_true", help="i XTTS-v2 a Chatterbox (musí běžet jejich kontejnery)")
    ap.add_argument("--ref", type=pathlib.Path, help="referenční vzorek pro klonování (10–20 s čisté řeči)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    models = _request("GET", "/v1/audio/models?kind=tts")
    print("TTS modely:")
    for m in models:
        print(f"  {m['name']:26s} {'OK ' if m['available'] else '-- '} {m['license']:.40s}  "
              f"{'komerčně' if m['commercial'] else 'ne-komerčně'}  {m['detail'][:60] if not m['available'] else ''}")

    cases = list(CPU_CASES)
    if args.gpu:
        cases += GPU_CASES
        if args.ref:
            upload_voice(args.ref, "smoke-ref")
            cases += [
                ("chatterbox_en_clone", {"text": EN, "language": "en", "voice": "smoke-ref", "seed": 42}),
                ("xtts_cs_clone", {"text": CS, "language": "cs", "voice": "smoke-ref", "engine": "xtts",
                                   "commercial_only": False, "seed": 42}),
            ]
    print("\nSyntéza:")
    failures = run(cases)
    print(f"\nVýstupy: {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
