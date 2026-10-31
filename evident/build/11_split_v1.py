"""Step 11 (v1): database-level train/dev/test splits and question selection for DREDGE v1 (configs/v1.json).

  test  = every BIRD-dev and Spider-dev database + 34 BIRD-train databases; 1,000 questions stratified like the pilot,
          INCLUDING the 200 pilot questions (their records, gold answers and pilot split are kept as they are)
  dev   = 8 BIRD-train databases; a stratified sample for tuning
  train = 27 BIRD-train databases; every valid question
BIRD-train databases are dealt to the three splits along their table-count order, so each split gets small and large
schemas. Spider-train databases are never a question source (they are the test-only decoy pool).
Every selected gold query is executed (read-only, time limit); failures, timeouts and empty answers are dropped and logged.

Test and dev candidates are oversampled (configs/v1.json `oversample`) and carry `pick_rank`; build/12 keeps the first
quota of candidates whose evidence passes build/03-04, so quotas are met exactly in stratified order.

Usage: python build/11_split_v1.py [--only test dev train] [--workers 8] [--smoke]
Output: data/v1/{test,dev,train}_candidates.jsonl, data/v1/db_splits.json, reports/v1/split_summary.json
"""
import argparse
import collections
import hashlib
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import paths, selection  # noqa: E402

CFG = json.load(open(paths.ROOT / "configs" / "v1.json"))


def deal_bird_train(dbs_by_tables, rng):
    """Labels interleaved in proportion (34/8/27) along the table-count order; ties shuffled. Deterministic."""
    counts = CFG["bird_train_db_split"]
    labels = sorted(((i + 0.5) / n, s) for s, n in counts.items() for i in range(n))
    order = sorted(dbs_by_tables, key=lambda d: (dbs_by_tables[d], rng.random()))
    assert len(order) == len(labels), (len(order), len(labels))
    return {d: s for d, (_pos, s) in zip(order, labels)}


def execute(recs, workers):
    """Run gold SQL for recs in parallel; returns results in input order."""
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda r: selection.run_gold(r["source"], r["db_id"], r["gold_sql"], CFG["exec_timeout_s"]), recs))


def finish(r, rows, split):
    r["split"] = split
    r["gold_answer"] = rows[: CFG["max_gold_rows"]]
    r["gold_n_rows"] = len(rows)
    r["gold_answer_sha1"] = hashlib.sha1(repr(rows).encode()).hexdigest()
    return r


def take(cands, n, split, workers, dropped):
    """Execute candidates in pick order (in batches) and keep the first n with a non-empty answer."""
    kept, i = [], 0
    while len(kept) < n and i < len(cands):
        batch = cands[i: i + max(workers * 4, n - len(kept))]
        i += len(batch)
        for r, (rows, err) in zip(batch, execute(batch, workers)):
            if len(kept) == n:
                break
            if err or not rows:                    # empty answers are ambiguous gold: skip
                dropped.append({"qid": r["qid"], "split": split, "reason": err or "empty result"})
                continue
            kept.append(finish(r, rows, split))
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", default=["test", "dev", "train"])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--smoke", action="store_true", help="tiny quotas, output to data/v1/smoke/ (tests the code, not a release)")
    a = ap.parse_args()
    out_dir = paths.DATA_V1 / "smoke" if a.smoke else paths.DATA_V1
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.smoke:
        CFG["test_quota"] = {k: v // 10 for k, v in CFG["test_quota"].items()}
        CFG["dev_sample"] = 15
    rng = random.Random(CFG["seed"])
    inv = [json.loads(l) for l in open(paths.DATA_V1 / "inventory.jsonl")]
    valid = [r for r in inv if not r["sql_error"] and r.get("reads_agree", False) and r["n_tables"] >= CFG["min_tables_read"]]

    bt_tables = {}
    for t in json.load(open(paths.BIRD_TRAIN / "train_tables.json")):
        bt_tables[t["db_id"]] = len(t["table_names_original"])
    bt_split = deal_bird_train({d: n for d, n in bt_tables.items() if any(r["db_id"] == d for r in inv)}, rng)

    def split_of(r):
        return "test" if r["origin"] in ("bird_dev", "spider_dev") else bt_split[r["db_id"]]

    db_splits = {f"{r['source']}/{r['db_id']}": split_of(r) for r in inv}
    json.dump({"bird_train": bt_split, "all": db_splits}, open(out_dir / "db_splits.json", "w"), indent=1, sort_keys=True)

    dropped, out = [], {}
    if "test" in a.only:
        pilot = [json.loads(l) for l in open(paths.DATA / "pilot.jsonl")][: 20 if a.smoke else None]
        for r in pilot:
            r["pilot_split"], r["split"], r["origin"] = r["split"], "test", paths.origin(r["source"], r["db_id"])
            r["in_pilot"] = True
        pilot_ids = {r["qid"] for r in pilot}
        test = list(pilot)
        for org, quota in CFG["test_quota"].items():
            need = quota - sum(r["origin"] == org for r in pilot)
            pool = [r for r in valid if r["origin"] == org and split_of(r) == "test" and r["qid"] not in pilot_ids]
            cands = selection.pick(pool, len(pool), random.Random(f"{CFG['seed']}:test:{org}"))
            got = take(cands, round(need * CFG["oversample"]), "test", a.workers, dropped)
            for i, r in enumerate(got):
                r["in_pilot"], r["pick_rank"] = False, i
            test += got
            print(f"test/{org}: {sum(r['origin'] == org for r in test)} candidates for quota {quota}", flush=True)
        out["test"] = test
    if "dev" in a.only:
        pool = [r for r in valid if split_of(r) == "dev"]
        cands = selection.pick(pool, len(pool), random.Random(f"{CFG['seed']}:dev"))
        out["dev"] = take(cands, round(CFG["dev_sample"] * CFG["oversample"]), "dev", a.workers, dropped)
        for i, r in enumerate(out["dev"]):
            r["pick_rank"] = i
        print(f"dev: {len(out['dev'])}", flush=True)
    if "train" in a.only:
        pool = sorted((r for r in valid if split_of(r) == "train"), key=lambda r: r["qid"])[: 30 if a.smoke else None]
        out["train"] = take(pool, len(pool), "train", a.workers, dropped)
        for i, r in enumerate(out["train"]):
            r["pick_rank"] = i
        print(f"train: {len(out['train'])}", flush=True)

    for s, recs in out.items():
        with open(out_dir / f"{s}_candidates.jsonl", "w") as f:
            for r in recs:
                f.write(json.dumps(r, default=str) + "\n")
    # leakage check: no database may appear in two splits
    seen = collections.defaultdict(set)
    for s, recs in out.items():
        for r in recs:
            seen[f"{r['source']}/{r['db_id']}"].add(s)
    summ = {
        "config": CFG,
        "bird_train_dbs_per_split": collections.Counter(bt_split.values()),
        "bird_train_tables_per_split": {s: sum(bt_tables[d] for d, x in bt_split.items() if x == s) for s in CFG["bird_train_db_split"]},
        "questions": {s: len(r) for s, r in out.items()},
        "databases": {s: len({(x["source"], x["db_id"]) for x in r}) for s, r in out.items()},
        "by_origin": {s: collections.Counter(x["origin"] for x in r) for s, r in out.items()},
        "by_operation": {s: collections.Counter(x["operation"] for x in r) for s, r in out.items()},
        "by_n_tables": {s: collections.Counter(min(x["n_tables"], 4) for x in r) for s, r in out.items()},
        "pilot_in_test": sum(x.get("in_pilot", False) for x in out.get("test", [])),
        "dbs_in_two_splits": sorted(k for k, v in seen.items() if len(v) > 1),
        "dropped": collections.Counter(f"{d['split']}/{d['reason'][:40]}" for d in dropped),
    }
    summ = json.loads(json.dumps(summ, default=str))
    json.dump(summ, open(out_dir / "split_summary.json" if a.smoke else paths.REPORTS / "v1" / "split_summary.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summ.items() if k != "config"}, indent=1))


if __name__ == "__main__":
    main()
