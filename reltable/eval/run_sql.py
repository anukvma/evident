"""Run SQL read-only against the benchmark databases (for checking gold vs alternative queries by hand).

  python eval/run_sql.py --id bird-113:0                  # gold AND alternative for a row of reports/alt_ref_review.csv, side by side
  python eval/run_sql.py --qid bird-113                   # just the gold query of a pilot question
  python eval/run_sql.py --db bird/financial --sql "SELECT COUNT(*) FROM loan"      # any query on any database
Options: --limit N (rows to print, default 10)  --timeout S (default 60)
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402
from reltable.evaluation import alt_refs  # noqa: E402
from reltable.evaluation.gold import canon_hash, load_gold, norm_cell, set_hash  # noqa: E402
from reltable.minidb import lenient  # noqa: E402


def run(db_path, sql, limit, timeout):
    con = lenient(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True))
    deadline = time.time() + timeout
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 200000)
    t0 = time.time()
    try:
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchall()
    except sqlite3.Error as e:
        print(f"   ERROR: {e}")
        return None
    finally:
        con.close()
    print(f"   {len(rows)} row(s) in {time.time() - t0:.2f}s   columns: {cols}")
    for r in rows[:limit]:
        print("   ", tuple(r))
    if len(rows) > limit:
        print(f"    ... {len(rows) - limit} more")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id"), ap.add_argument("--qid"), ap.add_argument("--db"), ap.add_argument("--sql")
    ap.add_argument("--limit", type=int, default=10), ap.add_argument("--timeout", type=int, default=60)
    a = ap.parse_args()
    if a.db:
        src, _, name = a.db.partition("/")
        if not a.sql:
            sys.exit("--db needs --sql")
        run(paths.sqlite_path(src, name), a.sql, a.limit, a.timeout)
        return
    gold = load_gold()
    q, _, idx = (a.id or a.qid or "").partition(":")
    if q not in gold:
        sys.exit("give --id <qid>:<i>, --qid <qid>, or --db <source>/<db_id> --sql '...'")
    g = gold[q]
    print(f"{q}  [{g.source}/{g.db_id}]  {g.operation}\nQUESTION: {g.question}")
    if g.knowledge:
        print(f"KNOWLEDGE: {g.knowledge}")
    print(f"\nGOLD SQL:\n   {' '.join(g.gold_sql.split())}")
    gr = run(g.db_path, g.gold_sql, a.limit, a.timeout)
    if a.id and idx != "":
        alt = alt_refs.load()[q]["alternatives"][int(idx)]
        print(f"\nALTERNATIVE {q}:{idx}  (status: {alt['status']})\n   {' '.join(alt['sql'].split())}")
        ar = run(g.db_path, alt["sql"], a.limit, a.timeout)
        if gr is not None and ar is not None:
            same_multi = canon_hash(gr) == canon_hash(ar)
            same_set = set_hash(gr) == set_hash(ar)
            print(f"\nSAME RESULT?  multiset: {'YES' if same_multi else 'NO'}   set: {'YES' if same_set else 'NO'}")
            print("   NOTE: every proposal reproduces the gold answer BY CONSTRUCTION, so YES is expected and proves nothing.\n"
                  "   The question for adjudication is whether the alternative has the same MEANING (same conditions, same relationship).\n"
                  "   Probe the parts that differ, e.g. with --db and --sql, and compare what each query actually selects.")


if __name__ == "__main__":
    main()
