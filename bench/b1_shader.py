"""B1 + B5 (PLAN-model-bench.md §3): MirrorBooth ShaderGen (v2 Fáze 0, inspire
mode, no RAG) with one model as every role, 12 style prompts. Per run:
validation passed (contract regex; glslang if installed), retries used,
ranker returned parseable JSON, wall time. The .frag files stay in the
pipeline output dir for B2 (impellerc) and B3/B4 (render + VL) later.

    python3 bench/b1_shader.py --model bench-qwen36 [--pipeline ~/bench-mirrorbooth/pipeline]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from common import KEY, RESULTS, append_jsonl, done_ids, guard, log

STYLES = [
    ("oil_warm", "warm oil painting look with visible brush strokes"),
    ("kaleido", "six-fold kaleidoscope mirror centred on the face"),
    ("comic_ink", "comic book ink outlines with halftone dots"),
    ("thermal", "thermal camera false-colour heat map"),
    ("vhs", "worn VHS tape: chroma bleed, scanlines, slight wobble over time"),
    ("pixel8", "8-bit pixel art with a limited 16-colour palette"),
    ("watercolor", "soft watercolor wash with paper texture"),
    ("neon_edges", "glowing neon edges on a dark background"),
    ("underwater", "underwater caustics rippling over the image over time"),
    ("pencil", "graphite pencil sketch, cross hatching"),
    ("glitch", "digital glitch blocks and RGB split, animated"),
    ("mosaic", "stained glass mosaic with dark lead lines"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--pipeline", type=Path, default=Path.home() / "bench-mirrorbooth" / "pipeline")
    args = ap.parse_args()
    out = RESULTS / args.model / "b1.jsonl"
    done = done_ids(out)
    env = {**os.environ, "SPARK_BASE_URL": "http://localhost:8080/v1", "SPARK_MODEL": args.model, "SPARK_API_KEY": KEY}
    py = str(args.pipeline / ".venv" / "bin" / "python")
    for key, style in STYLES:
        rid = f"b1-{key}"
        if rid in done:
            continue
        guard()
        name = f"{key}_{args.model.replace('-', '_')}"
        t0 = time.monotonic()
        p = subprocess.run([py, "run.py", "--style", style, "--name", name], cwd=args.pipeline, env=env, capture_output=True, text=True, timeout=1800)
        sec = round(time.monotonic() - t0)
        runs = sorted((args.pipeline / "output").glob(f"filter_{name}_*"))
        row: dict = {"id": rid, "style": style, "exit": p.returncode, "s": sec}
        if runs:
            d = runs[-1]
            v = json.loads((d / "validation.json").read_text()) if (d / "validation.json").exists() else {}
            rank = json.loads((d / "rank_report.json").read_text()) if (d / "rank_report.json").exists() else {}
            row |= {"dir": str(d), "passed": v.get("validation_passed"), "retries": v.get("retry_count"), "errors": (v.get("validation_errors") or [])[:3],
                    "rank_json": bool(rank.get("scores")), "overall": rank.get("overall")}
        else:
            row["stderr"] = p.stderr[-500:]
        append_jsonl(out, [row])
        log(f"{args.model} {rid}: passed={row.get('passed')} rank_json={row.get('rank_json')} {sec}s")


if __name__ == "__main__":
    main()
