"""Step 2: pick a stratified pilot, assign database-level splits, record gold answers.

Strata = operation x table-count bucket. We round-robin across strata, and inside a stratum
across databases, so no single database or operation dominates. Splits are by (source, db_id).
Every selected gold query is executed; failures/timeouts are dropped and logged.

Usage: python build/02_select_pilot.py
Output: data/pilot.jsonl, reports/pilot_summary.json
"""
import collections
import hashlib
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402

CFG = json.load(open(paths.ROOT / "configs" / "pilot.json"))


def run_gold(source, db_id, sql):
    con = sqlite3.connect(f"file:{paths.sqlite_path(source, db_id)}?mode=ro", uri=True)
    deadline = time.time() + CFG["exec_timeout_s"]
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 100000)
    try:
        rows = con.execute(sql).fetchall()
        return rows, None
    except sqlite3.Error as e:
        return None, str(e)
    finally:
        con.close()


def split_databases(rows, rng):
    dbs = sorted({(r["source"], r["db_id"]) for r in rows})
    split = {}
    for src in sorted({s for s, _ in dbs}):
        mine = [d for d in dbs if d[0] == src]
        rng.shuffle(mine)
        k = max(1, round(len(mine) * CFG["test_db_fraction"]))
        for i, d in enumerate(mine):
            split[d] = "test" if i < k else "dev"
    return split


def pick(pool, n, rng):
    strata = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in pool:
        strata[(r["operation"], min(r["n_tables"], 4))][r["db_id"]].append(r)
    for by_db in strata.values():
        for lst in by_db.values():
            rng.shuffle(lst)
    keys = sorted(strata)
    chosen, i = [], 0
    while len(chosen) < n and any(strata[k] for k in keys):
        by_db = strata[keys[i % len(keys)]]
        i += 1
        if not by_db:
            continue
        db = rng.choice(sorted(by_db))
        chosen.append(by_db[db].pop())
        if not by_db[db]:
            del by_db[db]
    return chosen


def main():
    rng = random.Random(CFG["seed"])
    inv = [json.loads(l) for l in open(paths.DATA / "inventory.jsonl")]
    inv = [r for r in inv if not r["sql_error"] and r["n_tables"] >= CFG["min_tables_read"]]
    split = split_databases(inv, rng)
    out, dropped = [], []
    for src, n in CFG["n_per_source"].items():
        pool = [r for r in inv if r["source"] == src]
        chosen = pick(pool, n * 2, rng)          # oversample, then drop failures
        kept = 0
        for r in chosen:
            if kept == n:
                break
            rows, err = run_gold(src, r["db_id"], r["gold_sql"])
            if err or not rows:                  # empty answers are ambiguous gold: skip
                dropped.append({"qid": r["qid"], "reason": err or "empty result"})
                continue
            r["split"] = split[(src, r["db_id"])]
            r["gold_answer"] = rows[: CFG["max_gold_rows"]]
            r["gold_n_rows"] = len(rows)
            r["gold_answer_sha1"] = hashlib.sha1(repr(rows).encode()).hexdigest()
            out.append(r)
            kept += 1
    with open(paths.DATA / "pilot.jsonl", "w") as f:
        for r in out:
            f.write(json.dumps(r, default=str) + "\n")
    summ = {
        "n": len(out), "dropped": dropped,
        "by_source_split": collections.Counter(f"{r['source']}/{r['split']}" for r in out),
        "by_operation": collections.Counter(r["operation"] for r in out),
        "by_n_tables": collections.Counter(min(r["n_tables"], 4) for r in out),
        "databases_per_split": {s: len({(r["source"], r["db_id"]) for r in out if r["split"] == s})
                                for s in ("dev", "test")},
        "db_split_overlap": sorted(set(
            d for d in {(r["source"], r["db_id"]) for r in out if r["split"] == "dev"}
        ) & {(r["source"], r["db_id"]) for r in out if r["split"] == "test"}),
    }
    summ = {k: ({str(a): b for a, b in v.items()} if isinstance(v, dict) else v)
            for k, v in summ.items()}
    json.dump(summ, open(paths.REPORTS / "pilot_summary.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
