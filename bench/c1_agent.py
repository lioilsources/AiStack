"""C1 (PLAN-model-bench.md §3): the lawyer agent (WorldLibraryProject
rag/agent, branch `pravnik`) with a real model over its 22 scenarios.

Run on SPARK with the WorldLibraryProject venv (openai, psycopg_pool):

    cd ~/deploy/WorldLibraryProject/rag && .venv/bin/python ~/deploy/AiStack/bench/c1_agent.py --model bench-qwen36

draft: the user opens with the document type, then answers whatever the
agent asks from the scenario's `odpovedi`, three at a time (like the app's
cards), until the agent renders or refuses; illegal scenarios must NOT
render. review: the contract text goes in as a document; the agent must call
review_document and name the planted defect.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS, URL, KEY, append_jsonl, done_ids, guard, log, thermal_event  # noqa: E402

RAG = Path.home() / "deploy" / "WorldLibraryProject" / "rag"
sys.path.insert(0, str(RAG))
sys.path.insert(0, str(RAG / "eval" / "lawyer_agent"))

TYP_CS = {
    "najemni_smlouva_byt": "nájemní smlouvu na byt", "kupni_smlouva_movita_vec": "kupní smlouvu na movitou věc",
    "smlouva_o_dilo": "smlouvu o dílo", "nda": "dohodu o mlčenlivosti", "pracovni_smlouva": "pracovní smlouvu",
    "dohoda_o_provedeni_prace": "dohodu o provedení práce", "dohoda_o_pracovni_cinnosti": "dohodu o pracovní činnosti",
    "vypoved_z_pracovniho_pomeru": "výpověď z pracovního poměru", "plna_moc": "plnou moc",
    "smlouva_o_poskytovani_sluzeb": "smlouvu o poskytování služeb", "licencni_smlouva_software": "licenční smlouvu k software",
    "reklamace": "reklamaci", "predzalobni_vyzva": "předžalobní výzvu", "odstoupeni_od_smlouvy_na_dalku": "odstoupení od smlouvy uzavřené na dálku",
}
MAX_KOL = 10


REVIEW_MUSI = {  # what a useful answer names, per planted defect (scenarios_review.jsonl)
    "najem_kauce_5x": ("2254", "trojnás"), "najem_stary_vzor_2239": ("2239",), "vymyslene_paragrafy": ("9998", "12345"),
    "chybi_cena_dila": ("cen",), "dpp_400_hodin": ("300",), "zkusebni_doba_6_mesicu": ("4 měsíc", "čtyř", "čtyři"),
    "vypoved_bez_duvodu": ("důvod", "§ 52"), "nda_bez_vyjimek": ("výjim",), "citace_bez_zakona": ("předpis", "zákon"),
}
FALESNY_POPLACH = ("neexist", "vymyšl", "neplatn")


def run_draft(agent, sc: dict) -> dict:
    sid = f"bench-{sc['id']}-{uuid.uuid4().hex[:6]}"
    zbyva = dict(sc["odpovedi"])
    zprava = f"Potřebuji připravit {TYP_CS.get(sc['typ'], sc['typ'])}."
    historie: list[dict] = []
    volani, ms, rendered, chyby_nastroju, dotlaceno = [], 0, False, 0, False
    kola = 0
    for kola in range(1, MAX_KOL + 1):
        guard()
        t0 = time.monotonic()
        k = agent.krok(zprava, session_id=sid, historie=historie)
        ms += int((time.monotonic() - t0) * 1000)
        volani += [v.get("nastroj") for v in k.volani]
        chyby_nastroju += sum(1 for v in k.volani if v.get("chyba"))
        historie += [{"role": "user", "content": zprava}, {"role": "assistant", "content": k.odpoved or ""}]
        if k.dokument:
            rendered = True
            break
        if not zbyva:
            if dotlaceno:
                break
            zprava, dotlaceno = "To je vše, co vím. Připrav prosím dokument.", True
            continue
        davka = dict(list(zbyva.items())[:3])
        for key in davka:
            zbyva.pop(key)
        zprava = "Údaje: " + "; ".join(f"{key} = {v}" for key, v in davka.items())
    c = sc["ceka"]
    ok = rendered == c.get("hotovo", True)
    if not c.get("hotovo", True):
        # a refusal must name the rule it hits (tool output carries it; the model should say it)
        ok = ok and c["porusuje"].lower() in " ".join(h["content"] for h in historie if h["role"] == "assistant").lower()
    return {"ok": ok, "rendered": rendered, "ceka": c, "kola": kola, "volani": volani, "chyby_nastroju": chyby_nastroju, "ms": ms, "posledni": historie[-1]["content"][-400:]}


def run_review(agent, sc: dict) -> dict:
    guard()
    t0 = time.monotonic()
    k = agent.krok(f"Zkontroluj prosím tuto smlouvu a řekni mi, co je v ní špatně:\n\n{sc['text']}", session_id=f"bench-{sc['id']}-{uuid.uuid4().hex[:6]}")
    ms = int((time.monotonic() - t0) * 1000)
    volani = [v.get("nastroj") for v in k.volani]
    text = (k.odpoved or "").lower()
    checks = {"review_document": "review_document" in volani}
    if musi := REVIEW_MUSI.get(sc["id"]):
        checks["jmenuje_vadu"] = any(m.lower() in text for m in musi)
    else:  # the clean contract: no invented problems
        checks["bez_falesneho_poplachu"] = not any(w in text for w in FALESNY_POPLACH)
    return {"ok": all(checks.values()), "checks": checks, "volani": volani, "ms": ms, "odpoved": (k.odpoved or "")[:600]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--law-url", default="http://localhost:8098")
    args = ap.parse_args()

    from run import load_dotenv, scenare  # rag/eval/lawyer_agent/run.py
    import os
    from psycopg_pool import ConnectionPool
    from agent.llm import OpenAIKlient
    from agent.loop import Pravnik
    from agent.session import Sessions
    from agent.tools import PravniNastroje

    load_dotenv()
    pool = ConnectionPool(os.environ["LAW_PG_DSN"], min_size=1, max_size=3, open=True)
    sessions = Sessions(pool)
    nastroje = PravniNastroje(args.law_url, pool, sessions)
    agent = Pravnik(nastroje, sessions=sessions, llm=OpenAIKlient(URL, args.model, api_key=KEY, timeout=600), router="heuristika")

    out = RESULTS / args.model / "c1.jsonl"
    done = done_ids(out)
    thermal_event(f"bench start: {args.model} [c1]")
    try:
        for kind, fn in (("draft", run_draft), ("review", run_review)):
            for sc in scenare(f"scenarios_{kind}.jsonl"):
                rid = f"c1-{kind}-{sc['id']}"
                if rid in done:
                    continue
                try:
                    r = fn(agent, sc)
                except Exception as e:  # a model that breaks the loop is a result, not a crash
                    r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                append_jsonl(out, [{"id": rid, "kind": kind, **r}])
                log(f"{args.model} {rid}: {'OK' if r.get('ok') else 'FAIL'} {r.get('error', '')}")
    finally:
        thermal_event(f"bench end: {args.model} [c1]")
        pool.close()


if __name__ == "__main__":
    main()
