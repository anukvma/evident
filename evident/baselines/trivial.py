"""Reference "systems" that exist to TEST THE HARNESS: each has a known, exact expected score.

  oracle              gold SQL + gold evidence                      -> everything perfect
  noop                outputs nothing                               -> no_answer
  random_tables       random table ranking, no SQL                  -> chance-level retrieval, no_answer
  all_rows            oracle SQL; fetches the whole required tables -> row_recall 1, precision < 1, sufficient
  miss_table          wrong SQL; selects all required tables but one-> failure missed_table
  sql_error           broken SQL, perfect retrieval                 -> failure sql_failure
  wrong_answer        wrong SQL, perfect retrieval                  -> wrong_answer_despite_evidence
  incomplete_rows     wrong SQL, perfect retrieval except half the rows -> incomplete_evidence

Usage: python baselines/trivial.py --out data/predictions [--limit N]
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident.evaluation import variants  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402
from evident.evaluation.schema import DEFAULT_LEVELS  # noqa: E402


def retrieval(g, level, rng, rows="gold"):
    cands = variants.candidate_tables(g, level)
    others = [t for t in cands if t.lower() not in {x.lower() for x in g.required_tables}]
    rng.shuffle(others)
    out = {"ranked_tables": list(g.required_tables) + others,
           "selected_tables": list(g.required_tables),
           "selected_columns": {t: list(c) for t, c in g.required_columns.items()},
           "join_keys": [list(p) for p in sorted(g.required_joins)]}
    if rows == "gold":
        out["retrieved_rows"] = {t: sorted(v) for t, v in g.row_ids.items()}
    elif rows == "half":
        out["retrieved_rows"] = {t: sorted(v)[: max(0, len(v) // 2)] for t, v in g.row_ids.items()}
    elif rows == "all":
        out["retrieved_rows"] = {t: "ALL" for t in g.required_tables}
    return out


def make(name, g, level, rng):
    base = {"system": name, "qid": g.qid, "level": level,
            "cost": {"latency_s": 0.01, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0}}
    if name == "oracle":
        return {**base, "sql": g.gold_sql, **retrieval(g, level, rng)}
    if name == "noop":
        return base
    if name == "random_tables":
        cands = variants.candidate_tables(g, level)
        rng.shuffle(cands)
        return {**base, "ranked_tables": cands, "selected_tables": cands[:5]}
    if name == "all_rows":
        return {**base, "sql": g.gold_sql, **retrieval(g, level, rng, rows="all")}
    if name == "miss_table":
        r = retrieval(g, level, rng)
        req = {t.lower() for t in g.required_tables}
        spare = [t for t in variants.candidate_tables(g, level) if t.lower() not in req]
        keep = g.required_tables[1:] if len(g.required_tables) > 1 else spare[:1]   # may be [] if none exist
        r["selected_tables"] = list(keep)
        r["ranked_tables"] = [t for t in r["ranked_tables"] if t not in g.required_tables[:1]] + g.required_tables[:1]
        return {**base, "sql": "SELECT 1", **{k: r[k] for k in ("ranked_tables", "selected_tables")}}
    if name == "sql_error":
        return {**base, "sql": "SELECT FROM WHERE", **retrieval(g, level, rng)}
    if name == "wrong_answer":
        return {**base, "sql": "SELECT 'no such answer'", **retrieval(g, level, rng)}
    if name == "incomplete_rows":
        return {**base, "sql": "SELECT 'no such answer'", **retrieval(g, level, rng, rows="half")}
    raise ValueError(name)


NAMES = ["oracle", "noop", "random_tables", "all_rows", "miss_table", "sql_error", "wrong_answer",
         "incomplete_rows"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--levels", nargs="+", type=int, default=list(DEFAULT_LEVELS))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gold = load_gold(a.limit)
    for name in NAMES:
        with open(out / f"{name}.jsonl", "w") as f:
            for g in gold.values():
                for lvl in a.levels:
                    rng = random.Random(f"{name}:{g.qid}:{lvl}")
                    f.write(json.dumps(make(name, g, lvl, rng)) + "\n")
        print("wrote", name, flush=True)


if __name__ == "__main__":
    main()
