"""Add `evidence_sufficient_eq` (equivalence-aware sufficiency) to an existing per_question.jsonl WITHOUT re-running the scorer.

Only rows that are strictly insufficient AND whose question has a human-confirmed alternative reference are recomputed (from the
retrieved rows in the prediction files); every other row copies the strict value. Typically a few dozen rows.

  python eval/rescore_sufficiency.py --per-question reports/eval_pilot_v1/per_question.jsonl \\
         --predictions data/predictions/*.jsonl --out reports/eval_pilot_v2
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident.evaluation import alt_refs  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402
from evident.evaluation.metrics import row_metrics  # noqa: E402
from evident.evaluation.schema import Prediction  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-question", required=True)
    ap.add_argument("--predictions", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.per_question)]
    conf = {q for q in alt_refs.load() if alt_refs.confirmed(q)}
    todo = {(r["system"], r["qid"], r["level"]) for r in rows
            if r["qid"] in conf and r.get("evidence_sufficient") == 0.0}
    gold = load_gold()
    preds = {}
    for f in a.predictions:
        for line in open(f):
            if line.strip():
                d = json.loads(line)
                if (d["system"], d["qid"], d["level"]) in todo:
                    preds[(d["system"], d["qid"], d["level"])] = Prediction.from_json(d)
    flipped = 0
    for r in rows:
        k = (r["system"], r["qid"], r["level"])
        if k in preds:
            r["evidence_sufficient_eq"] = row_metrics(gold[r["qid"]], preds[k], r["level"])["evidence_sufficient_eq"]
            flipped += r["evidence_sufficient_eq"] == 1.0
        else:
            r["evidence_sufficient_eq"] = r.get("evidence_sufficient", float("nan"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "per_question.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(conf)} questions with confirmed alternatives; {len(todo)} rows recomputed; {flipped} became sufficient.\n"
          f"wrote {out}/per_question.jsonl")


if __name__ == "__main__":
    main()
