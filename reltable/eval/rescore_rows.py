"""Re-score only SELECTED rows of an existing per_question.jsonl (no need to score everything again).

Use after (a) re-running failed model calls, or (b) a scorer fix that changes how some rows are labelled:
  python eval/rescore_rows.py --per-question reports/eval_final_v4/per_question.jsonl \\
         --predictions data/predictions/*.gemini-2.5-flash.jsonl data/predictions/*.gemini-2.5-flash.L250_500.jsonl \\
         --status retrieval_only system_error no_answer --out reports/eval_final_v5
Rows whose exec_status is in --status are scored again with the CURRENT scorer from the latest matching prediction line
(last line wins); all other rows are copied unchanged. Prints what changed.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable.evaluation.gold import load_gold  # noqa: E402
from reltable.evaluation.metrics import score  # noqa: E402
from reltable.evaluation.schema import Prediction  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-question", required=True)
    ap.add_argument("--predictions", nargs="+", required=True)
    ap.add_argument("--status", nargs="+", default=["retrieval_only", "system_error", "no_answer"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.per_question)]
    todo = {(r["system"], r["qid"], r["level"]) for r in rows if r["exec_status"] in a.status and not r["system"] == "question_blind"}
    preds = {}
    for f in a.predictions:
        for line in open(f):
            if line.strip():
                d = json.loads(line)
                k = (d["system"], d["qid"], d["level"])
                if k in todo:
                    preds[k] = Prediction.from_json(d)          # later lines overwrite earlier ones
    gold = load_gold()
    changed = []
    for i, r in enumerate(rows):
        k = (r["system"], r["qid"], r["level"])
        if k in todo and k in preds:
            new = score(gold[r["qid"]], preds[k], r["level"])
            if (new["exec_status"], new["failure"]) != (r["exec_status"], r["failure"]) or new["answer_correct"] != r["answer_correct"]:
                changed.append((k, r["exec_status"], new["exec_status"], new["failure"]))
            new.setdefault("evidence_sufficient_eq", r.get("evidence_sufficient_eq", new.get("evidence_sufficient")))
            rows[i] = {**r, **new}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "per_question.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(todo)} rows considered, {len(changed)} changed:")
    for k, old, new, fail in changed:
        print(f"   {k[0]}  {k[1]}  L{k[2]}:  {old} -> {new} ({fail})")


if __name__ == "__main__":
    main()
