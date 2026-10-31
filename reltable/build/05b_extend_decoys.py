"""Step 5b: APPEND decoys so the benchmark also has levels 250 and 500, without touching levels <= 100.

Existing manifests are backed up to data/decoy_manifests.v1.jsonl. For each question the first 100 decoys are kept
byte-identical (asserted); 400 more are chosen with the same exclusions (own DB, same-id DBs, other split, Spider-dev DBs,
same-name or look-alike tables) and size matching, skipping tables already used. Prefix levels stay nested.

Verification for the NEW levels (250, 500), every question: gold answer unchanged, unique table names, valid foreign
keys, candidate count == own tables + level. Also reports how heavily the pool is reused.

Usage: python build/05b_extend_decoys.py            Output: data/decoy_manifests.jsonl (extended), reports/decoys_summary_ext.json
"""
import collections
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import decoys as D  # noqa: E402
from reltable import paths  # noqa: E402

CFG = json.load(open(paths.ROOT / "configs" / "decoys.json"))
N_BASE, N_EXT_TOTAL = 100, 500
NEW_LEVELS = [l for l in CFG["levels"] if l > N_BASE]


def canon(rows):
    return sorted(repr(r) for r in rows)


def main():
    pool = D.build_pool(paths.DATA / "decoy_pool.json")
    recs = [json.loads(l) for l in open(paths.DATA / "evidence_draft.jsonl")]
    split_of = {f"{r['source']}/{r['db_id']}": r["split"] for r in recs}
    cur = paths.DATA / "decoy_manifests.jsonl"
    bak = paths.DATA / "decoy_manifests.v1.jsonl"
    if not bak.exists():
        shutil.copy(cur, bak)
    old = {json.loads(l)["qid"]: json.loads(l) for l in open(bak)}   # always extend from the pristine v1
    sha_v1 = hashlib.sha1(open(bak, "rb").read()).hexdigest()

    ext_cfg = {**CFG, "levels": [N_EXT_TOTAL - N_BASE]}
    out = []
    for r in recs:
        m = old[r["qid"]]
        assert len(m["decoys"]) == N_BASE, "expected 100 existing decoys"
        skip = {(d["source"], d["db_id"], d["table"]) for d in m["decoys"]}
        names = [d["name"] for d in m["decoys"]]
        more, excl = D.select_decoys(pool, r, split_of, ext_cfg, n_max=N_EXT_TOTAL - N_BASE, skip=skip,
                                     used_names=names, tag=":ext1")
        assert len(more) == N_EXT_TOTAL - N_BASE, f"{r['qid']}: only {len(more)} extra decoys available"
        new = dict(m)
        new["decoys"] = m["decoys"] + more
        new["n_decoys_available"] = len(new["decoys"])
        assert new["decoys"][:N_BASE] == m["decoys"], "prefix changed!"
        out.append(new)

    gold = {json.loads(l)["qid"]: json.loads(l) for l in open(paths.DATA / "pilot.jsonl")}
    tally, fails, secs = collections.Counter(), [], []
    for m, r in zip(out, recs):
        own = pool[f"{m['source']}/{m['db_id']}"]
        src = sqlite3.connect(f"file:{paths.sqlite_path(m['source'], m['db_id'])}?mode=ro", uri=True)
        want = canon(src.execute(r["gold_sql"]).fetchall())
        src.close()
        # leakage checks on the new decoys
        for d in m["decoys"][N_BASE:]:
            k = f"{d['source']}/{d['db_id']}"
            if k == f"{m['source']}/{m['db_id']}" or d["db_id"] == m["db_id"]:
                tally["leak_own_db"] += 1
            if k in split_of and split_of[k] != m["split"]:
                tally["leak_other_split"] += 1
            if d["db_id"] in D.all_spider_dev_dbs() and k not in split_of:
                tally["leak_unused_spider_dev"] += 1
        for lvl in NEW_LEVELS:
            ds = m["decoys"][:lvl]
            t0 = time.time()
            mem = D.materialise(pool, m["source"], m["db_id"], ds, CFG["row_cap"])
            try:
                got = canon(mem.execute(r["gold_sql"]).fetchall())
                ok_answer = got == want
            except sqlite3.Error as e:
                ok_answer = False
                fails.append((m["qid"], lvl, f"gold error: {e}"))
            present = {d["name"] for d in ds}
            fkbad = sum(1 for d in ds for (_i, _s, ref, *_r) in mem.execute(f'pragma foreign_key_list("{d["name"]}")')
                        if ref not in present)
            names = [t.lower() for t in own] + [d["name"].lower() for d in ds]
            total = mem.execute("select count(*) from sqlite_master where type='table'").fetchone()[0] + \
                mem.execute("select count(*) from orig.sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0]
            mem.close()
            secs.append(time.time() - t0)
            for name, ok in (("answer", ok_answer), ("namespace", len(names) == len(set(names))),
                             ("fk_valid", fkbad == 0), ("size", total == len(own) + lvl)):
                tally[f"L{lvl}_{name}_{'ok' if ok else 'FAIL'}"] += 1
                if not ok:
                    fails.append((m["qid"], lvl, name))
        print(m["qid"], flush=True)

    with open(cur, "w") as f:
        for m in out:
            f.write(json.dumps(m) + "\n")
    reuse = collections.Counter((d["source"], d["db_id"], d["table"]) for m in out for d in m["decoys"][:N_EXT_TOTAL])
    sizes = sorted(d["n_rows"] for m in out for d in m["decoys"][N_BASE:])
    jm = sorted(d["max_jaccard_required"] for m in out for d in m["decoys"][N_BASE:])
    summ = {"v1_manifest_sha1": sha_v1, "new_levels": NEW_LEVELS, "questions": len(out), "checks": dict(tally),
            "failures": fails[:20], "prefix_levels_le_100_unchanged": True,
            "pool_tables_used": len(reuse), "each_pool_table_in_n_variants(median)": sorted(reuse.values())[len(reuse) // 2],
            "new_decoy_rows_median": sizes[len(sizes) // 2], "max_jaccard_new_decoys_vs_required": jm[-1],
            "materialise_seconds_median": sorted(secs)[len(secs) // 2], "materialise_seconds_max": max(secs)}
    json.dump(summ, open(paths.REPORTS / "decoys_summary_ext.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
