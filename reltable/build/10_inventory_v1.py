"""Step 10 (v1): inventory of every candidate question for DREDGE v1: BIRD dev + Spider dev (reused from the pilot
inventory, data/inventory.jsonl) and BIRD train (analysed here). Spider-train databases are NOT a question source:
they are the test-only decoy pool (DESIGN.md, "v1 splits").

Analysis only: gold SQL is parsed and SQLite reports which tables/columns it reads; nothing is executed here.

Usage: python build/10_inventory_v1.py [--limit N]
Output: data/v1/inventory.jsonl, reports/v1/inventory_summary.json
"""
import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import inventory, paths  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="analyse only the first N BIRD-train questions (smoke test)")
    a = ap.parse_args()
    paths.DATA_V1.mkdir(parents=True, exist_ok=True)
    (paths.REPORTS / "v1").mkdir(parents=True, exist_ok=True)

    rows = []
    for r in map(json.loads, open(paths.DATA / "inventory.jsonl")):      # BIRD dev + Spider dev, already analysed
        r["origin"] = paths.origin(r["source"], r["db_id"])
        rows.append(r)
    train = list(inventory.load_bird_train())[: a.limit]
    t0 = time.time()
    for i, r in enumerate(inventory.analyse(train)):
        rows.append(r)
        if i % 500 == 0:
            print(f"bird_train {i}/{len(train)}  {time.time() - t0:.0f}s", flush=True)

    with open(paths.DATA_V1 / "inventory.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    ok = [r for r in rows if not r["sql_error"]]
    summ = {
        "total": len(rows),
        "by_origin": collections.Counter(r["origin"] for r in rows),
        "invalid_gold_sql_by_origin": collections.Counter(r["origin"] for r in rows if r["sql_error"]),
        "ast_vs_sqlite_table_disagreements": sum(not r.get("reads_agree", True) for r in ok),
        "databases_by_origin": {o: len({r["db_id"] for r in ok if r["origin"] == o})
                                for o in sorted({r["origin"] for r in ok})},
        "by_operation": collections.Counter(f"{r['origin']}/{r['operation']}" for r in ok),
        "by_n_tables": collections.Counter(f"{r['origin']}/{min(r['n_tables'], 4)}" for r in ok),
        "invalid_examples": [(r["qid"], str(r["sql_error"])[:120]) for r in rows if r["sql_error"]][:15],
    }
    json.dump(summ, open(paths.REPORTS / "v1" / "inventory_summary.json", "w"), indent=1, default=str)
    print(json.dumps(summ, indent=1, default=str))


if __name__ == "__main__":
    main()
