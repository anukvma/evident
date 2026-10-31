"""Robustness checks requested by review: split results, per-database effects, denominators, value-metric audit, completeness probe.
Reads a consolidated run; writes reports/review_checks.json (the paper generator turns it into macros). NO model calls.

  python eval/review_checks.py --final reports/eval_final_v6
"""
import argparse
import collections
import json
import sqlite3
import sys
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evident.evaluation import variants  # noqa: E402
from evident.evaluation.gold import load_gold, norm_cell, run_sql  # noqa: E402

SQL = ("oracle_schema_t2s", "fullschema_t2s", "bm25_t2s")


def mean(x):
    x = list(x)
    return sum(x) / len(x) if x else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", default=str(ROOT / "reports/eval_final_v6"))
    ap.add_argument("--second", default=str(ROOT / "reports/eval_final_gpt4o"))
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(Path(a.final) / "per_question.jsonl")]
    for r in rows:
        r["sys"] = r["system"].split("[")[0]
    R = lambda s, **k: [r for r in rows if r["sys"] == s and all(r[x] == y for x, y in k.items())]  # noqa: E731
    top = max(r["level"] for r in rows)
    out = {"top": top}
    # 1. development vs held-out databases
    out["split"] = {}
    for sp in ("dev", "test"):
        for s in SQL:
            lo, hi = R(s, level=0, split=sp), R(s, level=top, split=sp)
            out["split"][f"{sp}:{s}"] = {"n_db": len({r["db"] for r in lo}), "zero": mean(r["value_accuracy"] for r in lo),
                                         "top": mean(r["value_accuracy"] for r in hi)}
    # 2. per-database effect for BM25 (and full schema), leave-one-database-out, sign test
    def perdb(s):
        d = {}
        for db in {r["db"] for r in R(s)}:
            lo = [r["value_accuracy"] for r in R(s, level=0) if r["db"] == db]
            hi = [r["value_accuracy"] for r in R(s, level=top) if r["db"] == db]
            d[db] = (mean(hi) - mean(lo), len(lo))
        return d
    for s in ("bm25_t2s", "fullschema_t2s"):
        d = perdb(s)
        tot = sum(v * n for v, n in d.values()) / sum(n for _, n in d.values())
        lodo = [sum(v * n for k, (v, n) in d.items() if k != x) / sum(n for k, (v, n) in d.items() if k != x) for x in d]
        nz = [v for v, _ in d.values() if v != 0]
        k = sum(v < 0 for v in nz)
        p = sum(comb(len(nz), i) for i in range(k, len(nz) + 1)) / 2 ** len(nz) if nz else 1.0
        out[f"perdb:{s}"] = {"n_db": len(d), "decline": sum(v < 0 for v, _ in d.values()), "same": sum(v == 0 for v, _ in d.values()),
                             "improve": sum(v > 0 for v, _ in d.values()), "overall": tot, "lodo_min": min(lodo), "lodo_max": max(lodo),
                             "sign_p": p, "sign_k": k, "sign_n": len(nz)}
    # 3. missed-table share by level (BM25)
    out["bm25_missed_by_level"] = {str(l): mean(float(r["failure"] == "missed_table") for r in R("bm25_t2s", level=l))
                                   for l in sorted({r["level"] for r in rows})}
    out["bm25_missed_pooled"] = mean(float(r["failure"] == "missed_table") for r in R("bm25_t2s"))
    # 4. value-vs-strict audit over both models
    gold = load_gold()
    cases = []
    for tag, path, files in (("gemini", Path(a.final) / "per_question.jsonl",
                              ["data/predictions/%s.gemini-2.5-flash.jsonl", "data/predictions/%s.gemini-2.5-flash.L250_500.jsonl"]),
                             ("gpt4o", Path(a.second) / "per_question.jsonl", ["data/predictions/%s.openai_gpt-4o.jsonl"])):
        if not path.exists():
            continue
        pq = [json.loads(l) for l in open(path)]
        bad = {(r["system"], r["qid"], r["level"]) for r in pq if r["value_accuracy"] == 1.0 and r["exec_accuracy"] == 0.0}
        sql = {}
        for s in SQL:
            for f in files:
                p = ROOT / (f % s)
                if p.exists():
                    for l in open(p):
                        d = json.loads(l)
                        if (d["system"], d["qid"], d["level"]) in bad and d.get("sql"):
                            sql[(d["system"], d["qid"], d["level"])] = d["sql"]
        cases += [(k, v) for k, v in sql.items()]
        out.setdefault("n_sql_predictions", 0)
        out["n_sql_predictions"] += sum(1 for r in pq if r["sys"] if True and r["system"].split("[")[0] in SQL) if False else sum(1 for r in pq if r["system"].split("[")[0] in SQL)
    kc = collections.Counter()
    rk = lambda rr: collections.Counter(tuple(sorted(repr(norm_cell(c)) for c in r)) for r in rr)  # noqa: E731
    for (sysn, qid, lvl), sql in cases:
        g = gold[qid]
        con = variants.open_variant(g, lvl)
        try:
            pr = run_sql(con, sql, 60)
        except Exception:  # noqa: BLE001
            kc["error"] += 1
            continue
        finally:
            con.close()
        c = sqlite3.connect(f"file:{g.db_path}?mode=ro", uri=True)
        gr = c.execute(g.gold_sql).fetchall()
        c.close()
        kc["column_order_only" if rk(pr) == rk(gr) else "structure_differs"] += 1
    out["value_audit"] = {"value_ok_strict_wrong": len(cases), **dict(kc)}
    # 5. completeness probe
    pp = ROOT / "reports/completeness_probe.json"
    if pp.exists():
        d = json.load(open(pp))
        out["completeness"] = {"questions": d["questions"], "trials": d["questions"] * d["trials_per_question"],
                               "invariant": d["invariant_in_all_trials"], "changed": len(d["answer_changed"])}
    json.dump(out, open(ROOT / "reports/review_checks.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
