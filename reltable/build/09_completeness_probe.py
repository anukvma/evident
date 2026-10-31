"""Step 9: SEMANTIC COMPLETENESS probe for the gold evidence (separate from answer-sufficiency).

Step 4 shows the evidence rows REPRODUCE the gold answer (sufficiency) and that emptying a required table changes it (necessity of tables).
Neither shows the evidence is COMPLETE: a smaller set might reproduce the answer through one path while omitting rows the query semantically
depends on. This probe tests completeness by invariance: if the evidence contains every row the query's result depends on, then ADDING random
NON-evidence rows of the required tables must leave the gold answer unchanged. A change means the evidence omitted a row that matters (or the
query is non-deterministic, e.g. LIMIT without a total order -- those are reported separately and reviewed by hand).

For each question: evidence rows + a random fraction (5%, 25%, 50%; capped at 20,000 rows per table) of the other rows of each required table,
two seeds; gold SQL must return the gold answer every time.

Usage: python build/09_completeness_probe.py [--workers 4]      Output: reports/completeness_probe.json
"""
import argparse
import json
import random
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402
from reltable.evaluation.gold import canon_hash, load_gold, run_sql  # noqa: E402
from reltable.minidb import build_mini_db  # noqa: E402

FRACS, SEEDS, CAP = (0.05, 0.25, 0.5), (1, 2), 20000
_G = None


def init():
    global _G
    _G = load_gold()


def probe(qid):
    g = _G[qid]
    res = {"qid": qid, "rows_mode": g.rows_mode, "operation": g.operation, "trials": 0, "changed": [], "error": None}
    try:
        src = sqlite3.connect(f"file:{g.db_path}?mode=ro", uri=True)
        allids = {t: [r[0] for r in src.execute(f'SELECT rowid FROM "{t}"')] for t in g.required_tables}
        src.close()
        for frac in FRACS:
            for seed in SEEDS:
                rng = random.Random(f"{qid}:{frac}:{seed}")
                extra = {}
                for t in g.required_tables:
                    ev = g.row_ids.get(t, set())
                    others = [x for x in allids[t] if x not in ev]
                    k = min(CAP, int(len(others) * frac))
                    extra[t] = set(rng.sample(others, k)) if others and k else set()
                ids = {t: set(g.row_ids.get(t, set())) | extra[t] for t in g.required_tables}
                mini = build_mini_db(g.db_path, g.required_tables, ids)
                try:
                    ok = canon_hash(run_sql(mini, g.gold_sql, 120)) == g.answer_hash()
                finally:
                    mini.close()
                res["trials"] += 1
                if not ok:
                    res["changed"].append({"frac": frac, "seed": seed})
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    gold = load_gold()
    with ProcessPoolExecutor(max_workers=a.workers, initializer=init) as ex:
        out = []
        for i, r in enumerate(ex.map(probe, list(gold)), 1):
            out.append(r)
            if i % 25 == 0:
                print(f"  {i}/{len(gold)}", flush=True)
    bad = [r for r in out if r["changed"]]
    err = [r for r in out if r["error"]]
    summ = {"questions": len(out), "trials_per_question": len(FRACS) * len(SEEDS), "fractions": FRACS, "seeds": SEEDS, "cap_rows_per_table": CAP,
            "invariant_in_all_trials": sum(1 for r in out if not r["changed"] and not r["error"]),
            "answer_changed": [{"qid": r["qid"], "operation": r["operation"], "rows_mode": r["rows_mode"],
                                "changed_trials": len(r["changed"])} for r in bad],
            "errors": [{"qid": r["qid"], "error": r["error"]} for r in err]}
    json.dump(summ, open(paths.REPORTS / "completeness_probe.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summ.items() if k != "fractions"}, indent=1))


if __name__ == "__main__":
    main()
