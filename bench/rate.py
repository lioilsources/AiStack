"""Blind pairwise rating in the terminal (PLAN-model-bench.md §3, the human
half of A1 c/e, A2, A3). Two anonymous answers to the same prompt, you pick
the better one; order is shuffled, model names are revealed only in the tally.

    python3 bench/rate.py --suite a2            # rate
    python3 bench/rate.py --suite a2 --tally    # win rates per model

Needs results/<model>/<suite>.jsonl for ≥ 2 models (copy them from SPARK:
rsync -a spark:deploy/AiStack/bench/results/ bench/results/).
"""

from __future__ import annotations

import argparse
import itertools
import random
import textwrap
from collections import Counter

from common import BENCH, RESULTS, append_jsonl, read_jsonl

SUITES = {"a1": "a1_law", "a2": "a2_czech", "a3": "a3_translate"}
PROMPT_KEY = {"a1": "q", "a2": "prompt", "a3": "text"}
CRITERIA = {
    "a1": "Lepší odpověď Právníka: odliší zákon od výkladu (c) a je správně česky (e)?",
    "a2": "Lepší čeština: gramatika, rod, přirozenost, vhodnost pro zadání?",
    "a3": "Lepší překlad: věrnost a přirozená čeština pro děti?",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, choices=SUITES)
    ap.add_argument("--tally", action="store_true")
    args = ap.parse_args()
    out = BENCH / "results" / f"ratings_{args.suite}.jsonl"
    if args.tally:
        rows = read_jsonl(out) if out.exists() else []
        wins, games = Counter(), Counter()
        for r in rows:
            for m in (r["a"], r["b"]):
                games[m] += 1
            if r["pick"] in ("a", "b"):
                wins[r[r["pick"]]] += 1
            else:
                wins[r["a"]] += 0.5
                wins[r["b"]] += 0.5
        for m in sorted(games, key=lambda m: -wins[m] / games[m]):
            print(f"{m:24} {wins[m] / games[m]:.0%}  ({games[m]} srovnání)")
        return

    items = {r["id"]: r for r in read_jsonl(BENCH / "suites" / f"{SUITES[args.suite]}.jsonl")}
    answers = {m.name: {r["id"]: r.get("content") for r in read_jsonl(m / f"{args.suite}.jsonl") if r.get("content")}
               for m in RESULTS.iterdir() if (m / f"{args.suite}.jsonl").exists()}
    done = {(r["id"], frozenset((r["a"], r["b"]))) for r in read_jsonl(out)} if out.exists() else set()
    todo = [(i, a, b) for i in items for a, b in itertools.combinations(sorted(answers), 2)
            if i in answers[a] and i in answers[b] and (i, frozenset((a, b))) not in done]
    random.shuffle(todo)
    print(f"{len(todo)} dvojic; a = vlevo lepší, b = vpravo, = remíza, q = konec\n{CRITERIA[args.suite]}\n")
    for n, (i, a, b) in enumerate(todo, 1):
        if random.random() < 0.5:
            a, b = b, a
        print("=" * 100, f"\n[{n}/{len(todo)}] {items[i][PROMPT_KEY[args.suite]][:600]}\n")
        for tag, m in (("A", a), ("B", b)):
            print(f"--- {tag} ---\n" + "\n".join(textwrap.fill(p, 100) for p in answers[m][i].splitlines()) + "\n")
        pick = ""
        while pick not in ("a", "b", "=", "q"):
            pick = input("a / b / = / q > ").strip().lower()
        if pick == "q":
            break
        append_jsonl(out, [{"id": i, "a": a, "b": b, "pick": pick}])


if __name__ == "__main__":
    main()
