"""Step 13 (v1): decoy-table scale variants for one split and tier, with verification (configs/decoys_v1.json).

Tiers (same questions, same levels, nested within a tier):
  random  databases in random order, as in the pilot (same-source question databases interleaved with the rest)
  hard    tables most similar to the question first: reciprocal-rank fusion of (a) lexical schema similarity to the
          question's required tables (character n-gram TF-IDF over table and column names) and (b) semantic similarity
          between the question and the table schema (bge-small embeddings)
Pool rules (eligible_dbs_v1): test questions draw from other test databases + Spider-train databases; dev/train questions
only from databases of their own split. Rules shared with the pilot: no empty tables, no table named like an own table,
no column-name Jaccard >= 0.5 with an own table, row-count matching, at most row_cap rows per table.
Answer guard (v1, both tiers): a table is rejected if one of the gold query's string literals occurs in a column whose name
matches the column the gold query compares it with (token Jaccard >= col_sim), because such a table could be a second
source for the answer. A sample of hard-tier variants still needs a human spot check.

Verification per question (levels <= --verify-max-level, default 250; larger levels on a --verify-sample of questions):
answer (gold SQL returns the original multiset), namespace (unique names), fk_valid, size, split_leak (every decoy's
database is allowed for the question's split). Determinism: manifests are built twice and must be identical.

Usage: python build/13_decoys_v1.py --split test --tier random [--workers 4] [--limit N] [--smoke]
Output: data/v1/{split}_decoys_{tier}.jsonl, reports/v1/decoys_{split}_{tier}.json
"""
import argparse
import collections
import hashlib
import json
import random
import re
import sqlite3
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import decoys as D  # noqa: E402
from evident import paths  # noqa: E402

CFG = json.load(open(paths.ROOT / "configs" / "decoys_v1.json"))
TIMEOUT = 120


def words(s):
    return {w for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(s)).lower()) if w}


def col_match(a, b):
    wa, wb = words(a), words(b)
    return bool(wa | wb) and len(wa & wb) / len(wa | wb) >= CFG["col_sim"]


class Guard:
    """Rejects decoy tables that contain one of the question's compared string literals in a matching column."""

    def __init__(self, pool):
        self.pool, self.cache, self.cons = pool, {}, {}

    def _has(self, k, t, col, lit):
        key = (k, t, col, lit)
        if key not in self.cache:
            if k not in self.cons:
                self.cons[k] = D._ro(paths.sqlite_path(*k.split("/", 1)))
            op = "LIKE" if "%" in lit else "="
            sql = (f'SELECT 1 FROM (SELECT "{col}" AS v FROM "{t}" ORDER BY rowid LIMIT {CFG["row_cap"]}) '
                   f'WHERE lower(CAST(v AS TEXT)) {op} lower(?) LIMIT 1')
            try:
                self.cache[key] = self.cons[k].execute(sql, (lit,)).fetchone() is not None
            except sqlite3.Error:
                self.cache[key] = False
        return self.cache[key]

    def for_question(self, rec):
        lits = D.question_literals(rec["gold_sql"])

        def guard(k, t, m):
            return any(self._has(k, t, c, lit) for col, lit in lits for c in m["cols"] if col_match(col, c))
        return guard


def lexical_scores(pool, recs):
    """{qid: {(db_key, table): max cosine to a required table}} over character n-gram TF-IDF of table + column names."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from evident import embed
    keys = [(k, t) for k in sorted(pool) for t in sorted(pool[k])]
    texts = [embed.schema_text(t, pool[k][t]["cols"]) for k, t in keys]
    X = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True).fit_transform(texts)
    idx = {kt: i for i, kt in enumerate(keys)}
    out = {}
    for r in recs:
        own = f"{r['source']}/{r['db_id']}"
        req = [idx[(own, t)] for t in r["evidence"]["required"]["tables"] if (own, t) in idx]
        lex = (X @ X[req].T).max(axis=1).toarray().ravel() if req else [0.0] * len(keys)
        out[r["qid"]] = {kt: float(lex[i]) for kt, i in idx.items()}
    return keys, texts, out


def similarity_scores(pool, recs):
    """{qid: {(db_key, table): rrf score, "_lex": {...}}} for the hard tier: reciprocal-rank fusion of lexical schema
    similarity to the required tables and semantic similarity between the question and the table schema."""
    from evident import embed
    keys, texts, lex = lexical_scores(pool, recs)
    E = embed.encode(texts)
    Q = embed.encode([f"{r['question']} {r.get('knowledge') or ''}".strip() for r in recs], query=True)
    k = CFG["rrf_k"]
    out = {}
    for qi, r in enumerate(recs):
        sem = E @ Q[qi]
        lx = [lex[r["qid"]][kt] for kt in keys]
        rank_lex = {i: n for n, i in enumerate(sorted(range(len(keys)), key=lambda i: (-lx[i], i)))}
        rank_sem = {i: n for n, i in enumerate(sorted(range(len(keys)), key=lambda i: (-sem[i], i)))}
        out[r["qid"]] = {kt: 1 / (k + rank_lex[i]) + 1 / (k + rank_sem[i]) for i, kt in enumerate(keys)}
        out[r["qid"]]["_lex"] = lex[r["qid"]]
    return out


def build(pool, recs, db_split, tier, scores, lex):
    guard = Guard(pool)
    out = []
    for r in recs:
        sc = scores.get(r["qid"]) if scores else None
        dl, excl = D.select_decoys_v1(pool, r, db_split, CFG, tier, sc, guard.for_question(r))
        for d in dl:                                  # reported for both tiers, so the tiers can be compared
            d["lexical_sim_required"] = round(lex[r["qid"]][(f"{d['source']}/{d['db_id']}", d["table"])], 3)
        out.append({"qid": r["qid"], "source": r["source"], "db_id": r["db_id"], "split": r["split"], "tier": tier,
                    "own_tables": len(pool[f"{r['source']}/{r['db_id']}"]), "n_decoys_available": len(dl),
                    "levels_available": [l for l in CFG["levels"] if l <= len(dl)], "excluded": excl, "decoys": dl})
    return out


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


_W = {}


def _init(pool_path, allowed):
    _W["pool"], _W["allowed"] = D.build_pool(pool_path), allowed


def verify_one(args):
    m, gold_sql, levels = args
    pool, tally, fails = _W["pool"], collections.Counter(), []
    own = pool[f"{m['source']}/{m['db_id']}"]
    for d in m["decoys"]:
        if f"{d['source']}/{d['db_id']}" not in _W["allowed"][m["qid"]]:
            tally["split_leak_FAIL"] += 1; fails.append((m["qid"], "split_leak", d["db_id"]))
    ordered = "order by" in gold_sql.lower()
    src = D._ro(paths.sqlite_path(m["source"], m["db_id"]))
    want = canon(run(src, gold_sql), ordered)
    src.close()
    for lvl in levels:
        ds = m["decoys"][:lvl]
        mem = D.materialise(pool, m["source"], m["db_id"], ds, CFG["row_cap"])
        try:
            ok_answer = canon(run(mem, gold_sql), ordered) == want
        except sqlite3.Error as e:
            ok_answer = False; fails.append((m["qid"], lvl, f"gold error: {e}"))
        names = [t.lower() for t in own] + [d["name"].lower() for d in ds]
        present = {d["name"] for d in ds}
        fkbad = sum(ref not in present for d in ds for (_i, _s, ref, *_r) in mem.execute(f'pragma foreign_key_list("{d["name"]}")'))
        total = mem.execute("select count(*) from sqlite_master where type='table'").fetchone()[0] + \
            mem.execute("select count(*) from orig.sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0]
        mem.close()
        for name, ok in (("answer", ok_answer), ("namespace", len(names) == len(set(names))), ("fk_valid", fkbad == 0),
                         ("size", total == len(own) + lvl)):
            tally[f"{name}_{'ok' if ok else 'FAIL'}"] += 1
            if not ok:
                fails.append((m["qid"], lvl, name))
    return tally, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True, choices=["test", "dev", "train"])
    ap.add_argument("--tier", required=True, choices=["random", "hard"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None, help="first N questions only (smoke test; output to data/v1/smoke/)")
    ap.add_argument("--smoke", action="store_true", help="read the smoke split from data/v1/smoke/")
    ap.add_argument("--verify-max-level", type=int, default=250)
    ap.add_argument("--verify-sample", type=float, default=0.1, help="share of questions whose larger levels are verified")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    d = paths.DATA_V1 / "smoke" if (a.smoke or a.limit) else paths.DATA_V1
    out_path = d / f"{a.split}_decoys_{a.tier}.jsonl"
    if out_path.exists() and not a.force:
        sys.exit(f"{out_path} exists. Regenerating changes which decoys sit at each level and invalidates cached predictions; pass --force.")
    pool_path = paths.DATA_V1 / "decoy_pool.json"
    pool = D.build_pool(pool_path)
    recs = [json.loads(l) for l in open(d / f"{a.split}_evidence_draft.jsonl")][: a.limit]
    db_split = json.load(open(d / "db_splits.json"))["all"]
    scores = similarity_scores(pool, recs) if a.tier == "hard" else None

    lex = lexical_scores(pool, recs)[2]
    manifests = build(pool, recs, db_split, a.tier, scores, lex)
    h1 = hashlib.sha1(json.dumps(manifests, sort_keys=True).encode()).hexdigest()
    h2 = hashlib.sha1(json.dumps(build(pool, recs, db_split, a.tier, scores, lex), sort_keys=True).encode()).hexdigest()

    allowed = {r["qid"]: set(D.eligible_dbs_v1(pool, r, db_split)) for r in recs}
    rng = random.Random(CFG["seed"])
    sample = {r["qid"] for r in recs if rng.random() < a.verify_sample}
    jobs = [(m, r["gold_sql"], [l for l in m["levels_available"] if l <= a.verify_max_level or m["qid"] in sample])
            for m, r in zip(manifests, recs)]
    tally, fails = collections.Counter(), []
    with Pool(a.workers, initializer=_init, initargs=(pool_path, allowed)) as p:
        for i, (t, f) in enumerate(p.imap(verify_one, jobs)):
            tally += t; fails += f
            if i % 50 == 0:
                print(f"verified {i + 1}/{len(jobs)}", flush=True)

    with open(out_path, "w") as f:
        for m in manifests:
            f.write(json.dumps(m) + "\n")
    allj = sorted(x["max_jaccard_required"] for m in manifests for x in m["decoys"]) or [0]
    first100 = [x.get("lexical_sim_required") for m in manifests for x in m["decoys"][:100] if "lexical_sim_required" in x]
    summ = {
        "config": CFG, "split": a.split, "tier": a.tier, "n_questions": len(manifests),
        "deterministic": h1 == h2, "manifest_sha1": h1, "checks": dict(tally), "failures": fails[:30],
        "levels_available": collections.Counter(str(max(m["levels_available"], default=0)) for m in manifests),
        "exclusions_total": dict(sum((collections.Counter(m["excluded"]) for m in manifests), collections.Counter())),
        "decoy_vs_required_table_jaccard": {"median": allj[len(allj) // 2], "p99": allj[int(len(allj) * .99)], "max": allj[-1]},
        "first100_lexical_sim_required_mean": (sum(first100) / len(first100)) if first100 else None,
        "decoy_origin_mix": {f"{q}->{o}": n for (q, o), n in collections.Counter(
            (m["source"], paths.origin(x["source"], x["db_id"])) for m in manifests for x in m["decoys"][:100]).items()},
        "verified_large_levels_on": len(sample),
    }
    out_sum = (d / f"decoys_{a.split}_{a.tier}.json") if (a.smoke or a.limit) else paths.REPORTS / "v1" / f"decoys_{a.split}_{a.tier}.json"
    json.dump(json.loads(json.dumps(summ, default=str)), open(out_sum, "w"), indent=1)
    print(json.dumps({k: v for k, v in summ.items() if k != "config"}, indent=1, default=str))


if __name__ == "__main__":
    main()
