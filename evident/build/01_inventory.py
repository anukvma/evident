"""Step 1: normalise BIRD dev + Spider dev into one JSONL with SQL features.

Usage: python build/01_inventory.py
Output: data/inventory.jsonl, reports/inventory_summary.json
"""
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import paths, sqlast, sqlinfo  # noqa: E402


def load():
    for q in json.load(open(paths.BIRD / "dev.json")):
        yield dict(source="bird", qid=f"bird-{q['question_id']}", db_id=q["db_id"],
                   question=q["question"], knowledge=q.get("evidence", ""),
                   gold_sql=q["SQL"], source_difficulty=q.get("difficulty"))
    for i, q in enumerate(json.load(open(paths.SPIDER / "dev.json"))):
        yield dict(source="spider", qid=f"spider-dev-{i}", db_id=q["db_id"],
                   question=q["question"], knowledge="", gold_sql=q["query"],
                   source_difficulty=q.get("hardness"))


def main():
    paths.DATA.mkdir(exist_ok=True)
    paths.REPORTS.mkdir(exist_ok=True)
    rows, bad, analyzers, disagree = [], 0, {}, 0
    for r in load():
        key = (r["source"], r["db_id"])
        db = paths.sqlite_path(*key)
        if key not in analyzers:
            analyzers[key] = sqlast.Analyzer(sqlast.load_schema(db)[0])
        sqlite_reads, err = sqlinfo.read_set(db, r["gold_sql"])
        an = analyzers[key].analyze(r["gold_sql"])
        r["sql_error"] = err or (None if an["parse_ok"] else an["parse_error"])
        r["analysis"] = an
        if an["parse_ok"]:
            r["tables_read"] = an["reads"]
            # cross-check: SQLite's own view of which tables are read must match the AST's
            r["reads_agree"] = sorted(sqlite_reads) == sorted(an["reads"])
            disagree += not r["reads_agree"]
            r.update(sqlinfo.features(r["gold_sql"], an))
        bad += r["sql_error"] is not None
        rows.append(r)
    with open(paths.DATA / "inventory.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    ok = [r for r in rows if not r["sql_error"]]
    summ = {
        "total": len(rows), "invalid_gold_sql": bad, "ast_vs_sqlite_table_disagreements": disagree,
        "by_source": collections.Counter(r["source"] for r in ok),
        "by_operation": collections.Counter((r["source"], r["operation"]) for r in ok),
        "by_n_tables": collections.Counter((r["source"], min(r["n_tables"], 4)) for r in ok),
        "databases": len({(r["source"], r["db_id"]) for r in ok}),
    }
    summ = {k: ({str(a): b for a, b in v.items()} if isinstance(v, dict) else v)
            for k, v in summ.items()}
    json.dump(summ, open(paths.REPORTS / "inventory_summary.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
