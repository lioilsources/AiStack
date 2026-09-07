#!/usr/bin/env python3
"""Smoke test SFX: deset promptů z Kirian manifestu.

Cíl z plánu §2.4: SFX pod 5 s na kus.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import SFX_PROMPTS, AudioError, download, submit, wait  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / "bench" / "smoke_sfx"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name, prompt, duration in SFX_PROMPTS:
        started = time.monotonic()
        try:
            job_id = submit("sfx", {
                "prompt": prompt, "duration_s": duration,
                "mono": True, "format": "ogg", "seed": 42,
            })
            job = wait(job_id, timeout_s=300)
        except AudioError as exc:
            print(f"  {name:20s} SELHALO: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        out = job["outputs"][0]
        size = download(out, str(OUT / f"{name}.ogg"))
        flag = "OK " if elapsed < 5 else "POMALÉ"
        print(f"  {name:20s} {flag} {elapsed:6.1f}s  {out['duration']:5.2f}s audia  "
              f"{out['loudness_lufs']:6.1f} LUFS  {size/1024:6.0f} kB")
    print(f"\nVýstupy: {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
