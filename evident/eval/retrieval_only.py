"""Retrieval-only evaluation of the table retrievers (no LLM): for every question and level, rank the candidate tables
with BM25, dense, hybrid and graph-aware retrieval and report recall of the required tables in the top k, full coverage
(all required tables in the top k) and the missed-table rate of a top-5 prompt. Database-cluster bootstrap intervals.

  DREDGE_SPLIT=test DREDGE_TIER=random python eval/retrieval_only.py --levels 0 10 50 100 250 500 1000 --out reports/v1/retrieval_test_random.json
  python eval/retrieval_only.py --levels 0 100 --limit 20          # pilot, quick check

Uses data only; needs the local embedding model for dense/hybrid/graph (paths.EMBED_MODEL).
"""
import argparse
import collections
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baselines import common as C  # noqa: E402
from evident import paths  # noqa: E402
from evident.evaluation import variants  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402

METHODS = ("bm25", "dense", "hybrid", "graph")


def rank_all(g, level, con):
    cands = list({t.lower(): t for t in variants.candidate_tables(g, level)}.values())
    pm = C._table_meta(g, level)
    docs = {t: C.table_doc(con, t, pm[t.lower()][1]) for t in cands}
    bm, de = C.bm25_rank(g.question, g.knowledge, docs), C.dense_rank(g, level, con, cands)
    return {"bm25": bm, "dense": de, "hybrid": C.rrf([bm, de])[1], "graph": C.graph_rank(g, level, con, cands, docs)}


def boot(pairs, B=1000, seed=0):
    by = collections.defaultdict(list)
    for db, v in pairs:
        by[db].append(v)
    dbs, rng, bs = list(by), random.Random(seed), []
    for _ in range(B):
        s = [x for d in (rng.choice(dbs) for _ in dbs) for x in by[d]]
        bs.append(sum(s) / len(s))
    bs.sort()
    allv = [v for _, v in pairs]
    return {"mean": sum(allv) / len(allv), "lo": bs[int(.025 * B)], "hi": bs[int(.975 * B) - 1], "n": len(allv)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", nargs="+", type=int, default=[0, 10, 50, 100])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ks", nargs="+", type=int, default=[5, 10])
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    gold = load_gold(a.limit)
    act = paths.active()
    rows, t0 = [], time.time()
    for i, g in enumerate(gold.values()):
        avail = variants.manifest(g.qid).get("levels_available")
        for level in a.levels:
            if level and avail is not None and level not in avail:
                continue
            con = variants.open_variant(g, level)
            try:
                ranks = rank_all(g, level, con)
            finally:
                con.close()
            req = {t.lower() for t in g.required_tables}
            for m, r in ranks.items():
                low = [t.lower() for t in r]
                rec = {"qid": g.qid, "db": f"{g.source}/{g.db_id}", "level": level, "method": m,
                       "missed_table@5": float(not req <= set(low[:5]))}
                for k in a.ks:
                    rec[f"recall@{k}"] = len(req & set(low[:k])) / len(req)
                    rec[f"full_coverage@{k}"] = float(req <= set(low[:k]))
                rows.append(rec)
        if i % 25 == 0:
            print(f"{i + 1}/{len(gold)}  {time.time() - t0:.0f}s", flush=True)
    metrics = [k for k in rows[0] if k not in ("qid", "db", "level", "method")]
    out = {"split": act["split"], "tier": act["tier"], "n_questions": len(gold), "results": {}}
    for m in METHODS:
        for level in a.levels:
            sel = [r for r in rows if r["method"] == m and r["level"] == level]
            if sel:
                out["results"][f"{m}@{level}"] = {k: boot([(r["db"], r[k]) for r in sel]) for k in metrics}
    print(f"\n{'method':8} " + " ".join(f"+{l:<6}" for l in a.levels) + "   (recall@5)")
    for m in METHODS:
        print(f"{m:8} " + " ".join(f"{out['results'][f'{m}@{l}']['recall@5']['mean']:.3f}  " if f"{m}@{l}" in out["results"] else "  --    "
                                   for l in a.levels))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump(out, open(a.out, "w"), indent=1)
        with open(Path(a.out).with_suffix(".rows.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
