"""Per-question SQL analysis shared by the pilot inventory (build/01) and the v1 inventory (build/10)."""
from . import paths, sqlast, sqlinfo


def load_bird_dev():
    import json
    for q in json.load(open(paths.BIRD / "dev.json")):
        yield dict(source="bird", qid=f"bird-{q['question_id']}", db_id=q["db_id"],
                   question=q["question"], knowledge=q.get("evidence", ""),
                   gold_sql=q["SQL"], source_difficulty=q.get("difficulty"))


def load_spider_dev():
    import json
    for i, q in enumerate(json.load(open(paths.SPIDER / "dev.json"))):
        yield dict(source="spider", qid=f"spider-dev-{i}", db_id=q["db_id"],
                   question=q["question"], knowledge="", gold_sql=q["query"],
                   source_difficulty=q.get("hardness"))


def load_bird_train():
    """BIRD train has no question ids; the qid is the position in train.json (stable for this release)."""
    import json
    for i, q in enumerate(json.load(open(paths.BIRD_TRAIN / "train.json"))):
        yield dict(source="bird", qid=f"bird-train-{i}", db_id=q["db_id"],
                   question=q["question"], knowledge=q.get("evidence", ""),
                   gold_sql=q["SQL"], source_difficulty=None)


def analyse(records):
    """Add sql_error, analysis, tables_read, reads_agree and structural features to each record (yielded).
    Reads the schema and asks SQLite which tables/columns a query reads; never executes the query."""
    analyzers = {}
    for r in records:
        key = (r["source"], r["db_id"])
        db = paths.sqlite_path(*key)
        if key not in analyzers:
            analyzers[key] = sqlast.Analyzer(sqlast.load_schema(db)[0])
        sqlite_reads, err = sqlinfo.read_set(db, r["gold_sql"])
        try:
            an = analyzers[key].analyze(r["gold_sql"])
        except Exception as e:                           # BIRD train contains SQL the parser rejects; record, do not crash
            an = {"parse_ok": False, "parse_error": f"{type(e).__name__}: {e}"}
        r["origin"] = paths.origin(*key)
        r["sql_error"] = err or (None if an["parse_ok"] else an["parse_error"])
        r["analysis"] = an
        if an["parse_ok"]:
            r["tables_read"] = an["reads"]
            # cross-check: SQLite's own view of which tables are read must match the AST's
            r["reads_agree"] = sorted(sqlite_reads) == sorted(an["reads"])
            r.update(sqlinfo.features(r["gold_sql"], an))
        yield r
