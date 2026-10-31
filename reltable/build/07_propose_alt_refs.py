"""Propose / adjudicate alternative reference queries (equivalence-aware evidence sufficiency).

  # 1. collect proposals: predictions that were CORRECT but strictly insufficient (their SQL reproduces the gold answer)
  python build/07_propose_alt_refs.py --per-question reports/eval_final/per_question.jsonl \\
         --predictions data/predictions/*.jsonl
  # 2. a PERSON reads each pair (gold SQL vs alternative) and decides: same meaning -> confirm; right answer by coincidence -> reject
  python build/07_propose_alt_refs.py --confirm bird-1258 bird-113:0 bird-113:1 --reviewer "<name>"
  python build/07_propose_alt_refs.py --reject spider-dev-912 bird-113:2 --reviewer "<name>"
(`qid` = every alternative of that question; `qid:i` = only alternative number i, as printed in the review list.)

Only `confirmed` alternatives count in the scorer. Nothing is confirmed automatically.
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable.evaluation import alt_refs  # noqa: E402
from reltable.evaluation.gold import canon_hash, load_gold, run_sql  # noqa: E402
from reltable.minidb import lenient  # noqa: E402


def norm(sql):
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-question")
    ap.add_argument("--predictions", nargs="*", default=[])
    ap.add_argument("--confirm", nargs="*", default=[])
    ap.add_argument("--reject", nargs="*", default=[])
    ap.add_argument("--reviewer", default="")
    a = ap.parse_args()
    data = json.load(open(alt_refs.PATH)) if alt_refs.PATH.exists() else {}

    if a.confirm or a.reject:
        if not a.reviewer:
            sys.exit("--reviewer is required: an adjudication must be attributable to a person")
        for status, qids in (("confirmed", a.confirm), ("rejected", a.reject)):
            for tok in qids:
                q, _, idx = tok.partition(":")
                if q not in data:
                    sys.exit(f"{q}: no proposals; run the collection step first")
                alts = data[q]["alternatives"]
                if idx and not (idx.isdigit() and int(idx) < len(alts)):
                    sys.exit(f"{tok}: no such alternative (question has {len(alts)})")
                for i, alt in enumerate(alts):
                    if not idx or int(idx) == i:
                        alt["status"], alt["reviewed_by"] = status, a.reviewer
        json.dump(data, open(alt_refs.PATH, "w"), indent=1)
        print(f"updated {alt_refs.PATH}")
        return

    gold = load_gold()
    rows = [json.loads(l) for l in open(a.per_question)]
    cand = {(r["system"], r["qid"], r["level"]) for r in rows
            if r.get("answer_correct") == 1.0 and r.get("evidence_sufficient") == 0.0}
    sqls, req = {}, {}
    for f in a.predictions:
        for line in open(f):
            if not line.strip():
                continue
            p = json.loads(line)
            if (p["system"], p["qid"], p["level"]) in cand and p.get("sql"):
                sqls.setdefault(p["qid"], {}).setdefault(norm(p["sql"]), {"sql": p["sql"], "seen_in": set(), "read": p.get("read_tables")})
                sqls[p["qid"]][norm(p["sql"])]["seen_in"].add(p["system"])
    added = 0
    for q, alts in sqls.items():
        g = gold[q]
        have = {norm(x["sql"]) for x in data.get(q, {}).get("alternatives", [])}
        for k, v in alts.items():
            if k in have or k == norm(g.gold_sql):
                continue
            con = lenient(sqlite3.connect(f"file:{g.db_path}?mode=ro", uri=True))
            try:
                ok = canon_hash(run_sql(con, v["sql"], 60)) == g.answer_hash()
            except sqlite3.Error:
                ok = False
            con.close()
            if not ok:                      # must reproduce the gold answer on the full database
                continue
            read = {t.lower() for t in (v["read"] or [])}
            data.setdefault(q, {"alternatives": []})["alternatives"].append({
                "sql": v["sql"], "status": "proposed", "seen_in": sorted(v["seen_in"]),
                "skipped_tables": sorted(t for t in g.required_tables if t.lower() not in read) if read else None,
                "note": "", "reviewed_by": ""})
            added += 1
    json.dump(data, open(alt_refs.PATH, "w"), indent=1)
    print(f"{added} new proposals; {len(data)} questions in {alt_refs.PATH}\n")
    for q, d in data.items():
        g = gold[q]
        for i, alt in enumerate(d["alternatives"]):
            print(f"[{alt['status']}] {q}:{i}  skipped={alt.get('skipped_tables')}\n  Q    : {g.question[:140]}\n"
                  f"  GOLD : {' '.join(g.gold_sql.split())[:230]}\n  ALT  : {' '.join(alt['sql'].split())[:230]}\n")


if __name__ == "__main__":
    main()
