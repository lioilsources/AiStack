#!/usr/bin/env python3
"""Benchmark modelů → bench/timings.csv.

Projede každý dostupný model přes celou sadu promptů a zapíše časy, délky a
naměřenou hlasitost. Slouží k rozhodnutí, který model jde do produkce
(plán §2.4), takže píše CSV, ne hezký výpis.

  python3 scripts/bench.py                    # všechny dostupné modely
  python3 scripts/bench.py --kind sfx         # jen SFX
  python3 scripts/bench.py --models moss-soundeffect-v2,stable-audio-open-1.0
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import (  # noqa: E402
    MUSIC_PROMPTS,
    SFX_PROMPTS,
    AudioError,
    _request,
    download,
    submit,
    wait,
)

BENCH = pathlib.Path(__file__).resolve().parents[1] / "bench"
CSV_PATH = BENCH / "timings.csv"
COLUMNS = [
    "model", "kind", "prompt_id", "target_s", "wall_s", "audio_s",
    "lufs", "true_peak_db", "bytes", "seed", "error",
]


def available_models(kind: str | None, wanted: set[str] | None) -> list[dict]:
    models = _request("GET", "/v1/audio/models")
    out = []
    for m in models:
        if kind and m["kind"] != kind:
            continue
        if wanted and m["name"] not in wanted:
            continue
        if not m["available"]:
            print(f"  přeskakuji {m['name']}: {m['detail']}")
            continue
        out.append(m)
    return out


def run_case(model: dict, prompt_id: str, prompt: str, target_s: float, extra: dict) -> dict:
    row = {c: "" for c in COLUMNS}
    row.update(model=model["name"], kind=model["kind"], prompt_id=prompt_id, target_s=target_s)
    started = time.monotonic()
    try:
        payload = {"prompt": prompt, "duration_s": target_s, "seed": 42,
                   "format": "ogg", "model": model["name"], **extra}
        job = wait(submit(model["kind"], payload), timeout_s=1800)
    except AudioError as exc:
        row["wall_s"] = f"{time.monotonic() - started:.2f}"
        row["error"] = str(exc)[:200]
        return row

    out = job["outputs"][0]
    dest = BENCH / model["name"] / f"{prompt_id}.ogg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    row.update(
        wall_s=f"{time.monotonic() - started:.2f}",
        audio_s=f"{out['duration']:.2f}",
        lufs=f"{out['loudness_lufs']:.2f}",
        true_peak_db=f"{out['true_peak_db']:.2f}",
        bytes=download(out, str(dest)),
        seed=out["seed"],
    )
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["music", "sfx"])
    ap.add_argument("--models", help="čárkou oddělená jména modelů")
    args = ap.parse_args()
    wanted = set(args.models.split(",")) if args.models else None

    BENCH.mkdir(parents=True, exist_ok=True)
    models = available_models(args.kind, wanted)
    if not models:
        print("Žádný dostupný model — běží audio-music / audio-sfx?")
        return 1

    rows: list[dict] = []
    for model in models:
        print(f"\n=== {model['name']} ({model['kind']}, {model['license']})")
        cases = (
            [(pid, prompt, 30.0, {"bpm": bpm, "loop": True}) for pid, prompt, bpm in MUSIC_PROMPTS]
            if model["kind"] == "music"
            else [(pid, prompt, dur, {"mono": True}) for pid, prompt, dur in SFX_PROMPTS]
        )
        for prompt_id, prompt, target, extra in cases:
            row = run_case(model, prompt_id, prompt, target, extra)
            rows.append(row)
            status = row["error"] or f"{row['wall_s']}s → {row['audio_s']}s, {row['lufs']} LUFS"
            print(f"  {prompt_id:20s} {status}")

    # Append, ne overwrite: srovnání modelů má smysl napříč běhy.
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    print(f"\n{len(rows)} řádků → {CSV_PATH}")
    return 1 if any(r["error"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
