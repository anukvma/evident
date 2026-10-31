"""Question selection helpers shared by the pilot (build/02) and v1 (build/11): stratified picking and gold execution."""
import collections
import sqlite3
import time

from . import paths


def run_gold(source, db_id, sql, timeout_s):
    """Execute a gold query read-only with a wall-clock limit. Returns (rows, error)."""
    con = sqlite3.connect(f"file:{paths.sqlite_path(source, db_id)}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    deadline = time.time() + timeout_s
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 100000)
    try:
        return con.execute(sql).fetchall(), None
    except sqlite3.Error as e:
        return None, ("timeout" if "interrupted" in str(e) else str(e))
    finally:
        con.close()


def pick(pool, n, rng):
    """Round-robin over strata (operation x table-count bucket) and, inside a stratum, over databases, so no
    single database or operation dominates. Same procedure as the pilot."""
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
