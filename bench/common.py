"""Shared bits of the model bench (PLAN-model-bench.md §3–§4): an OpenAI-style
client against the LiteLLM gateway, a thermal guard, and JSONL I/O.

Runs on SPARK (gateway at localhost:8080, nvidia-smi and ~/ops/thermal.csv
local). Stdlib only, so any python3 on the box will do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BENCH = Path(__file__).resolve().parent
RESULTS = BENCH / "results"
URL = os.environ.get("BENCH_URL", "http://localhost:8080/v1")

# "Neuvař SPARKa": director nights peak at GPU 85 °C / zone 96 °C
# (~/ops/thermal.csv, 2026-09-26..28). The bench is interactive-sized load,
# so it pauses well below that and resumes only after a real cool-down.
HOT_GPU_C, HOT_ZONE_C = 80, 90
COOL_GPU_C, COOL_ZONE_C = 72, 82
THERMAL_CSV = Path.home() / "ops" / "thermal.csv"
THERMAL_EVENTS = Path.home() / "ops" / "thermal-events.csv"


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", file=sys.stderr, flush=True)


def _key() -> str:
    if k := os.environ.get("BENCH_KEY"):
        return k
    env = Path.home() / "deploy" / "AiStack" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    return "dummy"


KEY = _key()


def temps() -> tuple[float, float]:
    """(gpu °C, max thermal zone °C). Zone comes from the 5 s thermal logger;
    if it's stale or missing, only the GPU counts."""
    gpu = zone = 0.0
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout
        gpu = float(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    try:
        with THERMAL_CSV.open("rb") as f:
            f.seek(-400, os.SEEK_END)
            last = f.read().decode(errors="ignore").strip().splitlines()[-1].split(",")
        ts = datetime.strptime(last[0][:19], "%Y/%m/%d %H:%M:%S")
        if (datetime.now() - ts).total_seconds() < 60:
            zone = float(last[8])
    except (OSError, ValueError, IndexError):
        pass
    return gpu, zone


def guard() -> None:
    """Block while SPARK is hot."""
    gpu, zone = temps()
    if gpu < HOT_GPU_C and zone < HOT_ZONE_C:
        return
    log(f"thermal pause: GPU {gpu:.0f} °C, zone {zone:.0f} °C")
    while gpu > COOL_GPU_C or zone > COOL_ZONE_C:
        time.sleep(30)
        gpu, zone = temps()
    log(f"thermal resume: GPU {gpu:.0f} °C, zone {zone:.0f} °C")


def thermal_event(note: str) -> None:
    """Config changes go to ~/ops/thermal-events.csv (cooling A/B test log)."""
    try:
        with THERMAL_EVENTS.open("a", encoding="utf-8") as f:
            f.write(f'{datetime.now():%Y/%m/%d %H:%M:%S}.000,"{note}"\n')
    except OSError:
        pass


def chat(model: str, messages: list[dict], *, max_tokens: int = 1200, temperature: float | None = None, response_format: dict | None = None, tools: list[dict] | None = None, timeout: float = 600) -> dict:
    """One chat completion. Never raises: errors come back in `error`."""
    guard()
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    if response_format:
        body["response_format"] = response_format
    if tools:
        body["tools"], body["tool_choice"] = tools, "auto"
    req = urllib.request.Request(f"{URL}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}", "ms": int((time.monotonic() - t0) * 1000)}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"error": str(e), "ms": int((time.monotonic() - t0) * 1000)}
    ms = int((time.monotonic() - t0) * 1000)
    ch = d["choices"][0]
    msg = ch.get("message") or {}
    usage = d.get("usage") or {}
    return {
        "content": msg.get("content") or "",
        "tool_calls": msg.get("tool_calls") or [],
        "finish": ch.get("finish_reason"),
        "model": d.get("model"),
        "ms": ms,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "tok_s": round(usage["completion_tokens"] / (ms / 1000), 1) if usage.get("completion_tokens") and ms else None,
    }


def first_token_ms(model: str, messages: list[dict], max_tokens: int = 200) -> dict:
    """C3: does streaming work through the gateway, and how fast is the first token."""
    guard()
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True}
    req = urllib.request.Request(f"{URL}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.monotonic()
    first = None
    chunks = 0
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw in r:
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                delta = (json.loads(line[5:])["choices"] or [{}])[0].get("delta") or {}
                if delta.get("content") and first is None:
                    first = int((time.monotonic() - t0) * 1000)
                chunks += 1
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError) as e:
        return {"error": str(e)}
    return {"first_token_ms": first, "chunks": chunks, "total_ms": int((time.monotonic() - t0) * 1000)}


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def done_ids(p: Path) -> set[str]:
    return {r["id"] for r in read_jsonl(p)} if p.exists() else set()
