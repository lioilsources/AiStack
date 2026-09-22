#!/usr/bin/env python3
"""Vibe z předlohy z příkazové řádky — ladění i akceptace (plán vibe §3 krok 6, §4).

    python3 services/audio/scripts/vibe.py sample.mp3 --mode vibe --batch 3 --hint "more cinematic"
    python3 services/audio/scripts/vibe.py sample.mp3 --mode groove --strength 0.5
    python3 services/audio/scripts/vibe.py sample.mp3 --analyze-only
    python3 services/audio/scripts/vibe.py sample.mp3 --seed 7 --repeat 2   # determinismus

Vypíše analýzu, finální caption, časy a cesty k výstupům. Mluví s běžící
službou (AUDIO_URL, výchozí 127.0.0.1:8093), jen standardní knihovna.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import API_KEY, AUDIO_URL, AudioError, _request, download, wait  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / "bench" / "vibe"


def upload(path: pathlib.Path) -> dict:
    """Multipart ručně — urllib ho neumí a skript nemá mít závislosti."""
    import urllib.error
    import urllib.request

    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="sample"; filename="{path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{AUDIO_URL}/v1/audio/vibe/samples", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise AudioError(f"upload → HTTP {exc.code}: {exc.read()[:300]!r}") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sample", type=pathlib.Path)
    ap.add_argument("--mode", choices=("vibe", "groove"), default="vibe")
    ap.add_argument("--batch", type=int, default=2, help="počet variant (1–4)")
    ap.add_argument("--hint", default="", help="přání připojené ke captionu")
    ap.add_argument("--caption", default="", help="vlastní caption místo analýzy")
    ap.add_argument("--strength", type=float, default=0.5, help="groove: síla coveru")
    ap.add_argument("--duration", type=float, default=None, help="vibe: délka v s")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--lm-plan", action="store_true", help="vibe: struktura přes 5Hz LM")
    ap.add_argument("--repeat", type=int, default=1, help="generovat N× se stejným seedem")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--reanalyze", action="store_true", help="analyzovat i když už analýza je")
    args = ap.parse_args()

    started = time.monotonic()
    sample = upload(args.sample)
    sid = sample["sample_id"]
    print(f"předloha {sid}: {sample['source_duration_s']:.1f} s → úsek "
          f"{sample['window_start_s']:.1f}+{sample['duration_s']:.1f} s")

    analysis = sample.get("analysis")
    if analysis is None or args.reanalyze:
        t = time.monotonic()
        job = wait(_request("POST", "/v1/audio/vibe/analyze", {"sample_id": sid})["job_id"])
        analysis = job["result"]
        print(f"analýza {time.monotonic() - t:.1f} s")
    print(json.dumps({k: analysis[k] for k in (
        "genre", "bpm", "keyscale", "timesignature", "instrumental", "vocal_language", "source",
        "measured", "warnings")}, ensure_ascii=False, indent=1))
    print(f"caption: {analysis['caption']}")
    if args.analyze_only:
        return 0

    out_dir = OUT / f"{args.sample.stem}-{args.mode}-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    shas: list[list[str]] = []
    for rep in range(args.repeat):
        t = time.monotonic()
        payload = {
            "sample_id": sid, "mode": args.mode, "variations": args.batch,
            "user_hint": args.hint, "cover_strength": args.strength, "lm_plan": args.lm_plan,
        }
        if args.caption:
            payload["caption"] = args.caption
        if args.duration:
            payload["duration_s"] = args.duration
        if args.seed is not None:
            payload["seed"] = args.seed
        job = wait(_request("POST", "/v1/audio/vibe/generate", payload)["job_id"])
        manifest = job["result"]
        elapsed = time.monotonic() - t
        if rep == 0:
            print(f"\nfinální caption: {manifest['params']['caption']}")
        per = ", ".join(f"{v['generate_s']:.1f}" for v in manifest["variations"])
        print(f"\nběh {rep + 1}: {elapsed:.1f} s (generování po variantách: {per} s)")
        shas.append([o["sha256"] for o in job["outputs"]])
        for out in job["outputs"]:
            dest = out_dir / f"r{rep}-{out['filename']}"
            download(out, str(dest))
            print(f"  {dest}  {out['duration']:.1f} s  {out['loudness_lufs']:.1f} LUFS  seed={out['seed']}")
        (out_dir / f"r{rep}-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))

    if args.repeat > 1:
        same = all(s == shas[0] for s in shas[1:])
        print(f"\ndeterminismus (stejný seed, stejné parametry): {'SHODA' if same else 'ROZDÍL'}")
    print(f"\ncelkem {time.monotonic() - started:.1f} s, výstupy: {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AudioError as exc:
        print(f"CHYBA: {exc}", file=sys.stderr)
        raise SystemExit(1)
