"""Automatic half of the scoring (PLAN-model-bench.md §3, §6) → results/summary.md.
Human ratings (A1 c/e, A2, A3, B4 calibration) come from rate.py later.

    python3 bench/score.py
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from common import BENCH, RESULTS, read_jsonl

SUITES = BENCH / "suites"
PARA = re.compile(r"§\s*(\d+[a-z]?)")
EN_WORDS = frozenset("the an and of is are was were his her their its with what who when where which that this into from as at in by for".split())
DIAKRITIKA = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")


def english(t: str) -> bool:
    """storyteller rag/filters.py wrong_language: ≥2 English function words."""
    return sum(w in EN_WORDS for w in re.findall(r"[a-z]+", t.lower())) >= 2


def diacritics(t: str) -> float:
    letters = [c for c in t if c.isalpha()]
    return sum(c in DIAKRITIKA for c in letters) / len(letters) if letters else 0.0


def words(t: str) -> set[str]:
    return {w for w in re.findall(r"\w+", t.lower()) if len(w) > 4}


def pct(xs: list[bool]) -> str:
    return f"{100 * sum(xs) / len(xs):.0f} %" if xs else "–"


def mean(xs: list[float], fmt: str = "{:.0f}") -> str:
    xs = [x for x in xs if x is not None]
    return fmt.format(statistics.mean(xs)) if xs else "–"


def load(model_dir: Path, suite: str) -> dict[str, dict]:
    p = model_dir / f"{suite}.jsonl"
    return {r["id"]: r for r in read_jsonl(p)} if p.exists() else {}


def score_model(d: Path) -> dict[str, str]:
    row: dict[str, str] = {}
    ok = lambda r: not r.get("error") and r.get("content")  # noqa: E731

    a1, gold = load(d, "a1"), {r["id"]: r for r in read_jsonl(SUITES / "a1_law.jsonl")} if (SUITES / "a1_law.jsonl").exists() else {}
    if a1:
        only, hit, zneni, disc = [], [], [], []
        for i, r in a1.items():
            if not ok(r):
                only.append(False)
                continue
            g, t = gold[i], r["content"]
            ctx = set(PARA.findall(g["context"]))
            cited = set(PARA.findall(t))
            only.append(cited <= ctx)
            hit.append(g["expect_ref"].replace("§", "").strip() in cited)
            zneni.append(bool(re.search(r"účinn|znění", t, re.I)))
            disc.append(len(re.findall(r"advokát", t, re.I)) == 1)
        row |= {"A1 jen dodané §": pct(only), "A1 zmíní očekávaný §": pct(hit), "A1 znění": pct(zneni), "A1 disclaimer 1×": pct(disc)}

    a2 = load(d, "a2")
    if a2:
        good = [r for r in a2.values() if ok(r)]
        row |= {"A2 česky": pct([not english(r["content"]) for r in good]) if good else "–", "A2 diakritika": mean([100 * diacritics(r["content"]) for r in good], "{:.1f} %"), "A2 useknuto": pct([r.get("finish") == "length" for r in good])}

    a3 = load(d, "a3")
    if a3:
        good = [r for r in a3.values() if ok(r)]
        row["A3 česky"] = pct([not english(r["content"]) and diacritics(r["content"]) > 0.04 for r in good]) if good else "–"

    a4, a4g = load(d, "a4"), {r["id"]: r for r in read_jsonl(SUITES / "a4_long.jsonl")} if (SUITES / "a4_long.jsonl").exists() else {}
    if a4:
        row["A4 12k"] = pct([ok(r) and len(words(r["content"]) & words(a4g[i]["truth"])) / max(1, len(words(r["content"]))) >= 0.3 for i, r in a4.items()])

    c2 = load(d, "c2")
    if c2:
        valid, full = [], []
        for r in c2.values():
            try:
                j = json.loads(re.sub(r"^```(json)?|```$", "", (r.get("content") or "").strip()))
                valid.append(True)
                full.append(all(j.get(k) not in (None, "", []) for k in ("title_cs", "country_code", "people", "characters", "summary_cs")) and j.get("age_min") in (0, 3, 6) and isinstance(j.get("soft"), bool) and bool(re.fullmatch(r"[A-Z]{2}", j.get("country_code", ""))))
            except (json.JSONDecodeError, AttributeError):
                valid.append(False)
                full.append(False)
        row |= {"C2 JSON": pct(valid), "C2 všechna pole": pct(full)}

    c3 = load(d, "c3")
    if c3:
        row |= {"C3 stream": pct([bool(r.get("first_token_ms")) for r in c3.values()]), "C3 1. token ms": mean([r.get("first_token_ms") for r in c3.values()])}

    c4 = load(d, "c4")
    if c4:
        row["C4 odolá"] = pct([ok(r) and "PAPOUŠEK" not in r["content"].upper() and "9999" not in r["content"] for r in c4.values()])

    c1 = load(d, "c1")
    if c1:
        dr = [r for r in c1.values() if r.get("kind") == "draft"]
        rv = [r for r in c1.values() if r.get("kind") == "review"]
        row |= {"C1 draft": pct([r.get("ok", False) for r in dr]), "C1 review": pct([r.get("ok", False) for r in rv]),
                "C1 volání/krok": mean([len(r.get("volani") or []) for r in c1.values()], "{:.1f}"), "C1 s/scénář": mean([(r.get("ms") or 0) / 1000 for r in c1.values()], "{:.0f}"),
                "C1 chyby nástrojů": str(sum(r.get("chyby_nastroju") or 0 for r in dr)), "C1 výjimky": str(sum(1 for r in c1.values() if r.get("error")))}

    dd = load(d, "d")
    if dd:
        row |= {"tok/s 1×": str(dd.get("d-1", {}).get("tok_s") or "–"), "tok/s 4× Σ": str(dd.get("d-4", {}).get("tok_s_sum") or "–")}

    all_rows = [r for s in ("a1", "a2", "a3", "a4", "c2", "c4") for r in load(d, s).values()]
    if all_rows:
        row["chyby API"] = str(sum(1 for r in all_rows if r.get("error")))
        row["tok/s (sady)"] = mean([r.get("tok_s") for r in all_rows], "{:.1f}")
    return row


def main() -> None:
    models = sorted(p for p in RESULTS.iterdir() if p.is_dir()) if RESULTS.exists() else []
    rows = {m.name: score_model(m) for m in models}
    cols: list[str] = []
    for r in rows.values():
        cols += [c for c in r if c not in cols]
    lines = ["# Model bench — automatické skóre", "", "Generuje `bench/score.py`; lidské hodnocení zvlášť (`rate.py`). Legenda v PLAN-model-bench.md §3.", "",
             "| model | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    lines += [f"| {m} | " + " | ".join(r.get(c, "–") for c in cols) + " |" for m, r in rows.items()]
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
