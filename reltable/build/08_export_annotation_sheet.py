"""Export a stratified sample of pilot questions for HUMAN validation of the evidence labels (two annotators).

  python build/08_export_annotation_sheet.py --n 40
Writes data/annotation/sheet_annotatorA.csv and sheet_annotatorB.csv (identical; each annotator fills their own copy
independently), plus data/annotation/GUIDE.md. Afterwards: python eval/annotator_agreement.py
"""
import argparse
import collections
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402

GUIDE = """# Evidence-label validation guide

You see a question, its database, the reference SQL, and the evidence labels that were DERIVED AUTOMATICALLY from that SQL. Judge the
labels; do not rewrite them. Fill your own CSV copy independently; do not discuss with the other annotator until both are done.
Use the database (data/../bird or spider SQLite files) to check anything you are unsure about.

Fields (answer yes / no / unsure; free text only where stated):
* sql_correct      Does the reference SQL answer the question as asked? yes / no / ambiguous (the question or knowledge allows
                   more than one reasonable reading). If no or ambiguous, say why in `notes`.
* tables_ok        Are exactly the listed tables needed to answer the question? Use `tables_missing` / `tables_extra`
                   (comma-separated names) for corrections. A table is NOT needed if an equivalent query avoids it
                   (e.g. a bridge table reachable through a column already present): say `alt_route_possible = yes`.
* columns_ok       Are the listed columns the ones the answer depends on (including those used only to filter or join)?
* joins_ok         Are the listed join keys the right relationships between these tables? `n/a` if there is no join.
* rowmode_ok       The label says whether the whole qualifying row set is needed (`complete_set`: aggregation, ranking, negation,
                   set operations) or only the matching rows (`predicate`). Is the label right?
* alt_route_possible  Is there a different, equally valid query that reads a DIFFERENT set of tables? yes / no
* notes            Anything surprising.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    ev = [json.loads(l) for l in open(paths.DATA / "evidence_draft.jsonl")]
    strata = collections.defaultdict(list)
    for e in ev:
        strata[(e["source"], e["operation"])].append(e)
    rng = random.Random(a.seed)
    for v in strata.values():
        rng.shuffle(v)
    chosen, keys = [], sorted(strata)
    while len(chosen) < a.n and any(strata[k] for k in keys):          # round-robin over (source, operation) strata
        for k in keys:
            if strata[k] and len(chosen) < a.n:
                chosen.append(strata[k].pop())
    rng.shuffle(chosen)
    auto = ["qid", "source", "db_id", "operation", "question", "external_knowledge", "gold_sql", "auto_required_tables",
            "auto_required_columns", "auto_join_keys", "auto_row_mode"]
    human = ["sql_correct", "tables_ok", "tables_missing", "tables_extra", "columns_ok", "joins_ok", "rowmode_ok",
             "alt_route_possible", "notes"]
    out = paths.DATA / "annotation"
    out.mkdir(exist_ok=True)
    for who in ("A", "B"):
        with open(out / f"sheet_annotator{who}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(auto + human)
            for e in chosen:
                req = e["evidence"]["required"]
                w.writerow([e["qid"], e["source"], e["db_id"], e["operation"], e["question"], e["knowledge"],
                            " ".join(e["gold_sql"].split()), ", ".join(req["tables"]),
                            "; ".join(f"{t}: {', '.join(c)}" for t, c in req["columns"].items()),
                            "; ".join(" = ".join(k["pair"]) for k in req["join_keys"]) +
                            ("; " if req["join_keys"] and req["implicit_joins"] else "") +
                            "; ".join(f"{j['outer']} {j['kind']} {j['inner']}" for j in req["implicit_joins"]),
                            req["rows"]["mode"]] + [""] * len(human))
    (out / "GUIDE.md").write_text(GUIDE)
    print(f"wrote {len(chosen)} questions to {out}/sheet_annotator{{A,B}}.csv  "
          f"({collections.Counter(e['source'] for e in chosen)}, {collections.Counter(e['operation'] for e in chosen)})")


if __name__ == "__main__":
    main()
