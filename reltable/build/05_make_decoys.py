"""Step 5: build decoy-table scale variants for the pilot and VERIFY every variant.

Per question we draw one ordered decoy list (up to max level) and take prefixes for each level, so
levels are nested. Stored as manifests only (data/decoy_manifests.jsonl).

Verification for every (question, level):
  answer       gold SQL on the variant returns the same multiset as the original database
  namespace    all table names (own + decoy) unique, case-insensitively
  fk_valid     every foreign key in every decoy points at a table present in the variant
  no_leak      no decoy comes from the question's own DB, the same db_id, or the other split
  nested       decoys(level k) is a prefix of decoys(level k+1)
  size         total candidate tables == own tables + level
Also: determinism (manifests rebuilt twice are identical) and decoy look-alike statistics.

Usage: python build/05_make_decoys.py
Output: data/decoy_manifests.jsonl, reports/decoys_summary.json
"""
import collections
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import decoys as D  # noqa: E402
from reltable import paths  # noqa: E402

CFG = json.load(open(paths.ROOT / "configs" / "decoys.json"))
CFG["levels"] = CFG["base_levels"]            # levels 250/500 are appended by build/05b, never regenerated here
TIMEOUT = 120


def canon(rows, ordered):
    k = [repr(r) for r in rows]
    return k if ordered else sorted(k)


def run(con, sql):
    deadline = time.time() + TIMEOUT
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 200000)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.set_progress_handler(None, 0)


def main():
    if (paths.DATA / "decoy_manifests.jsonl").exists() and "--force" not in sys.argv:
        sys.exit("data/decoy_manifests.jsonl exists. Regenerating would change which decoys sit at each level and invalidate every "
                 "cached prediction. Use build/05b_extend_decoys.py to add levels, or pass --force if you really mean it.")
    pool = D.build_pool(paths.DATA / "decoy_pool.json")
    recs = [json.loads(l) for l in open(paths.DATA / "evidence_draft.jsonl")]
    split_of = {f"{r['source']}/{r['db_id']}": r["split"] for r in recs}
    gold = {json.loads(l)["qid"]: json.loads(l) for l in open(paths.DATA / "pilot.jsonl")}

    def build():
        out = []
        for r in recs:
            dl, excl = D.select_decoys(pool, r, split_of, CFG)
            out.append({"qid": r["qid"], "source": r["source"], "db_id": r["db_id"],
                        "split": r["split"], "own_tables": len(pool[f"{r['source']}/{r['db_id']}"]),
                        "n_decoys_available": len(dl), "excluded": excl, "decoys": dl})
        return out

    manifests = build()
    h1 = hashlib.sha1(json.dumps(manifests, sort_keys=True).encode()).hexdigest()
    h2 = hashlib.sha1(json.dumps(build(), sort_keys=True).encode()).hexdigest()

    tally, fails, short = collections.Counter(), [], []
    for m, r in zip(manifests, recs):
        own = pool[f"{m['source']}/{m['db_id']}"]
        if m["n_decoys_available"] < max(CFG["levels"]):
            short.append((m["qid"], m["n_decoys_available"]))
        # nested + leak checks on the full list
        for d in m["decoys"]:
            k = f"{d['source']}/{d['db_id']}"
            if k == f"{m['source']}/{m['db_id']}" or d["db_id"] == m["db_id"]:
                tally["leak_own_db"] += 1
            if k in split_of and split_of[k] != m["split"]:
                tally["leak_other_split"] += 1
            if d["db_id"] in D.all_spider_dev_dbs() and k not in split_of:
                tally["leak_unused_spider_dev"] += 1
        ordered = "order by" in r["gold_sql"].lower()
        want = canon(gold[m["qid"]]["gold_answer"] and
                     sqlite3.connect(f"file:{paths.sqlite_path(m['source'], m['db_id'])}?mode=ro", uri=True)
                     .execute(r["gold_sql"]).fetchall(), ordered)
        for lvl in CFG["levels"]:
            ds = m["decoys"][:lvl]
            if len(ds) < lvl:
                tally["level_unreachable"] += 1; continue
            mem = D.materialise(pool, m["source"], m["db_id"], ds, CFG["row_cap"])
            try:
                got = canon(run(mem, r["gold_sql"]), ordered)
                ok_answer = got == want
            except sqlite3.Error as e:
                ok_answer = False; fails.append((m["qid"], lvl, f"gold error: {e}"))
            names = [t.lower() for t in own] + [d["name"].lower() for d in ds]
            ok_ns = len(names) == len(set(names))
            fkbad = 0
            present = {d["name"] for d in ds}
            for d in ds:
                for (_i, _s, ref, *_r) in mem.execute(f'pragma foreign_key_list("{d["name"]}")'):
                    fkbad += ref not in present
            total = mem.execute("select count(*) from sqlite_master where type='table'").fetchone()[0] \
                + mem.execute("select count(*) from orig.sqlite_master where type='table' "
                              "and name not like 'sqlite_%'").fetchone()[0]
            ok_size = total == len(own) + lvl
            mem.close()
            for name, ok in (("answer", ok_answer), ("namespace", ok_ns), ("fk_valid", fkbad == 0),
                             ("size", ok_size)):
                tally[f"{name}_{'ok' if ok else 'FAIL'}"] += 1
                if not ok:
                    fails.append((m["qid"], lvl, name))
        print(m["qid"], flush=True)

    with open(paths.DATA / "decoy_manifests.jsonl", "w") as f:
        for m in manifests:
            f.write(json.dumps(m) + "\n")
    allj = sorted(d["max_jaccard_required"] for m in manifests for d in m["decoys"])
    src_mix = collections.Counter((m["source"], d["source"]) for m in manifests for d in m["decoys"])
    summ = {
        "config": CFG, "n_questions": len(manifests), "deterministic": h1 == h2, "manifest_sha1": h1,
        "checks": dict(tally), "failures": fails[:20],
        "questions_short_of_max_level": short,
        "decoys_per_question_available": {"min": min(m["n_decoys_available"] for m in manifests)},
        "own_tables_per_question": {str(k): v for k, v in sorted(collections.Counter(
            m["own_tables"] for m in manifests).items())},
        "exclusions_total": dict(sum((collections.Counter(m["excluded"]) for m in manifests),
                                     collections.Counter())),
        "decoy_vs_required_table_jaccard": {"median": allj[len(allj) // 2], "p99": allj[int(len(allj) * .99)],
                                             "max": allj[-1]},
        "decoy_source_mix(question_source,decoy_source)": {f"{a}->{b}": n for (a, b), n in src_mix.items()},
        "decoy_rows_kept_median": sorted(d["rows_kept"] for m in manifests for d in m["decoys"])[
            sum(len(m["decoys"]) for m in manifests) // 2],
    }
    json.dump(summ, open(paths.REPORTS / "decoys_summary.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
