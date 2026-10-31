"""Alternative-reference adjudication through a spreadsheet.

  python build/07c_alt_ref_csv.py export                       -> reports/alt_ref_review.csv  (open in Excel/Numbers/Sheets)
  python build/07c_alt_ref_csv.py apply --reviewer "<name>"     reads the `your_decision` column back into data/alt_reference_sql.json

`your_decision`: confirm | reject | (blank = leave as proposed). Only `confirm` rows count in the scorer. The last column holds the
assistant's suggestion for comparison; judge first, then look, or delete that column.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import paths, sqlast  # noqa: E402
from evident.evaluation import alt_refs  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402

CSV_PATH = paths.REPORTS / "alt_ref_review.csv"
SUGGEST = {   # assistant's reading, with the reason. Not authoritative.
    "bird-113:0": ("confirm", "skips account; loan.account_id = disp.account_id gives the same client"),
    "bird-113:1": ("confirm", "same as :0"),
    "bird-113:2": ("reject", "adds type = 'OWNER', a condition the question and gold do not have"),
    "bird-137:0": ("confirm", "district join is redundant: district_id is already on account"),
    "bird-137:1": ("reject", "uses the CLIENT's district, gold uses the ACCOUNT's district"),
    "bird-1258:0": ("confirm", "Patient is a bridge; Laboratory and Examination share ID"),
    "bird-1258:1": ("confirm", "same as :0"),
    "spider-dev-103:0": ("confirm", "models come from car_names instead of model_list (same set under the foreign key)"),
    "spider-dev-104:0": ("confirm", "same as spider-dev-103"),
    "spider-dev-104:1": ("confirm", "same as spider-dev-103"),
    "spider-dev-912:0": ("reject", "counts likes RECEIVED (liked_id) but gold counts likes GIVEN (student_id): right answer by coincidence"),
    "bird-1101:0": ("reject", "drops the 'Italy' condition"),
    "bird-1101:1": ("reject", "derives the country from the team instead of applying 'Italy'"),
    "bird-131:0": ("reject", "uses the CLIENT's district; gold uses the ACCOUNT's district"),
    "bird-131:1": ("reject", "joins loan.account_id to client.client_id (a wrong relationship) and counts loans instead of summing amounts"),
    "bird-994:0": ("reject", "reads results (driver points) instead of constructorResults; totals can differ"),
    "bird-1001:0": ("reject", "replaces the circuit name with 'Singapore Grand Prix' (outside knowledge)"),
}
for i in range(4):
    SUGGEST[f"bird-1451:{i}"] = ("confirm", "counts events through budget.link_to_event, so event and member add no condition (read :0 and :1; :2 and :3 follow the same pattern)")
for i in range(9):
    SUGGEST[f"bird-1526:{i}"] = ("confirm", "skips gasstations, an inner join that adds no condition; remaining differences are formula rewrites")
for i in range(3):
    SUGGEST[f"bird-1003:{i}"] = ("confirm", "applies statusId = 3 directly instead of joining status; MAX(count) equals the top-1 count")

LIT = re.compile(r"'((?:[^']|'')*)'|\"([^\"]*)\"|(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def literals(sql):
    out = set()
    for a, b, c in LIT.findall(re.sub(r"--[^\n]*", "", sql)):
        v = (a or b or c).strip().lower()
        if v and v not in {"0", "1", "1.0"}:
            out.add(v)
    return out


def export():
    data = json.load(open(alt_refs.PATH))
    gold, an = load_gold(), {}
    cols = ["id", "your_decision", "your_notes", "source", "db", "operation", "question", "knowledge", "gold_sql", "alternative_sql",
            "tables_only_in_gold", "tables_only_in_alternative", "literals_only_in_gold", "literals_only_in_alternative",
            "seen_in", "current_status", "assistant_suggestion", "assistant_reason"]
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for q, d in data.items():
            g = gold[q]
            key = (g.source, g.db_id)
            if key not in an:
                an[key] = sqlast.Analyzer(sqlast.load_schema(paths.sqlite_path(*key))[0])
            gr = set(an[key].analyze(g.gold_sql).get("reads", {}))
            gl = literals(g.gold_sql)
            for i, alt in enumerate(d["alternatives"]):
                ar = set(an[key].analyze(alt["sql"]).get("reads", {}))
                al = literals(alt["sql"])
                sg, why = SUGGEST.get(f"{q}:{i}", ("", ""))
                w.writerow([f"{q}:{i}", "", "", g.source, g.db_id, g.operation, g.question, g.knowledge,
                            " ".join(g.gold_sql.split()), " ".join(alt["sql"].split()),
                            ", ".join(sorted(gr - ar)), ", ".join(sorted(ar - gr)),
                            ", ".join(sorted(gl - al)), ", ".join(sorted(al - gl)),
                            ", ".join(s.split("[")[0] for s in alt.get("seen_in", [])), alt["status"], sg, why])
    print(f"wrote {CSV_PATH}  ({sum(len(d['alternatives']) for d in data.values())} rows). Fill the `your_decision` column, then run `apply`.")


def apply(reviewer):
    if not reviewer:
        sys.exit("--reviewer is required: an adjudication must be attributable to a person")
    data = json.load(open(alt_refs.PATH))
    counts = {"confirmed": 0, "rejected": 0, "left": 0}
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            dec = row["your_decision"].strip().lower()
            q, _, i = row["id"].partition(":")
            if not dec:
                counts["left"] += 1
                continue
            if dec not in ("confirm", "reject"):
                sys.exit(f"{row['id']}: your_decision must be confirm, reject or blank (got {row['your_decision']!r})")
            alt = data[q]["alternatives"][int(i)]
            if " ".join(alt["sql"].split()) != row["alternative_sql"]:
                sys.exit(f"{row['id']}: the SQL in the CSV no longer matches the proposal file; re-export before applying")
            alt["status"] = "confirmed" if dec == "confirm" else "rejected"
            alt["reviewed_by"], alt["note"] = reviewer, row["your_notes"].strip()
            counts[alt["status"]] += 1
    json.dump(data, open(alt_refs.PATH, "w"), indent=1)
    print(counts, f"-> {alt_refs.PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["export", "apply"])
    ap.add_argument("--reviewer", default="")
    a = ap.parse_args()
    export() if a.mode == "export" else apply(a.reviewer)
