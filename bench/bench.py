"""Runs suites against one LiteLLM alias (PLAN-model-bench.md §3). Resumable:
an item already in results/<alias>/<suite>.jsonl is skipped. Sequential on
purpose — one request at a time keeps the load (and SPARK's temperature)
at chat level; `guard()` pauses when hot anyway.

    python3 bench/bench.py --model bench-qwen36 --suites a1,a2,a3,a4,c2,c3,c4
    python3 bench/bench.py --model bench-qwen36 --suites d        # 4 parallel streams, tok/s
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
from pathlib import Path

from common import BENCH, RESULTS, append_jsonl, chat, done_ids, first_token_ms, log, read_jsonl, thermal_event

SUITES = BENCH / "suites"
PRAVNIK = Path.home() / "deploy" / "WorldLibraryProject" / "rag" / "prompts" / "pravnik_cs.md"


def law_messages(item: dict) -> list[dict]:
    return [
        {"role": "system", "content": PRAVNIK.read_text(encoding="utf-8")},
        {"role": "user", "content": f"Úryvky z předpisů:\n\n{item['context']}\n\nOtázka: {item['q']}"},
    ]


def build(suite: str, item: dict) -> tuple[list[dict], dict]:
    """(messages, extra chat kwargs) for one item."""
    if suite in ("a1", "c4"):
        return law_messages(item), {"max_tokens": 1500}
    if suite == "a2":
        return [{"role": "system", "content": "Odpovídej česky, přirozeně a gramaticky správně."}, {"role": "user", "content": item["prompt"]}], {"max_tokens": 900}
    if suite == "a3":
        return [{"role": "system", "content": "Jsi literární překladatel. Přelož text do přirozené, spisovné češtiny pro čtení dětem. Vrať jen překlad."}, {"role": "user", "content": item["text"]}], {"max_tokens": 1500}
    if suite == "a4":
        return [{"role": "system", "content": "Odpovídáš česky a jen z dodaného textu zákona."}, {"role": "user", "content": f"TEXT ZÁKONA:\n{item['doc']}\n\nOTÁZKA: {item['q']}"}], {"max_tokens": 500}
    if suite == "c2":
        return [
            {"role": "system", "content": "Extract a structured record of the folk tale. title_cs and summary_cs in Czech (summary max 3 sentences), other fields per schema. Output only JSON."},
            {"role": "user", "content": item["text"]},
        ], {"max_tokens": 800, "response_format": {"type": "json_schema", "json_schema": {"name": "tale", "schema": item["schema"], "strict": True}}}
    raise ValueError(suite)


FILES = {"a1": "a1_law", "a2": "a2_czech", "a3": "a3_translate", "a4": "a4_long", "c2": "c2_schema", "c4": "c4_injection"}


def run_suite(model: str, suite: str, temperature: float | None) -> None:
    out = RESULTS / model / f"{suite}.jsonl"
    if suite == "c3":
        done = done_ids(out)
        for it in read_jsonl(SUITES / "a2_czech.jsonl")[:10]:
            if it["id"] in done:
                continue
            r = first_token_ms(model, [{"role": "user", "content": it["prompt"]}])
            append_jsonl(out, [{"id": it["id"], **r}])
        return
    if suite == "d":
        # D: throughput — 1 stream, then 4 at once, same prompt, 400 tokens.
        msg = [{"role": "user", "content": "Napiš podrobný popis podzimního lesa, alespoň 400 slov."}]
        one = chat(model, msg, max_tokens=400, temperature=0.7)
        with cf.ThreadPoolExecutor(4) as ex:
            four = list(ex.map(lambda _: chat(model, msg, max_tokens=400, temperature=0.7), range(4)))
        append_jsonl(out, [{"id": "d-1", **{k: one.get(k) for k in ("tok_s", "ms", "completion_tokens", "error")}},
                           {"id": "d-4", "tok_s_each": [f.get("tok_s") for f in four], "tok_s_sum": round(sum(f.get("tok_s") or 0 for f in four), 1), "errors": [f.get("error") for f in four if f.get("error")]}])
        return
    items = read_jsonl(SUITES / f"{FILES[suite]}.jsonl")
    done = done_ids(out)
    for it in items:
        if it["id"] in done:
            continue
        messages, kw = build(suite, it)
        r = chat(model, messages, temperature=temperature, **kw)
        append_jsonl(out, [{"id": it["id"], **r}])
        status = r.get("error") or f"{r['ms']} ms, {r.get('tok_s')} tok/s"
        log(f"{model} {suite} {it['id']}: {status}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="LiteLLM alias, e.g. bench-qwen36")
    ap.add_argument("--suites", default="a1,a2,a3,a4,c2,c3,c4,d")
    ap.add_argument("--temperature", type=float, default=None, help="default: the alias's own (bench-* = 0.2)")
    args = ap.parse_args()
    thermal_event(f"bench start: {args.model} [{args.suites}]")
    try:
        for s in args.suites.split(","):
            log(f"== {args.model} {s}")
            run_suite(args.model, s.strip(), args.temperature)
    finally:
        thermal_event(f"bench end: {args.model}")
    log(f"done → {RESULTS / args.model}")


if __name__ == "__main__":
    main()
