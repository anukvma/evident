"""Score one or more prediction files.

Usage:
  python eval/run_eval.py --predictions preds_a.jsonl [preds_b.jsonl ...] --out reports/eval_run
         [--levels 0 10 50 100] [--limit N] [--no-ci]

Writes <out>/per_question.jsonl and <out>/summary.json. Systems are compared on the questions
and levels they all answer; a missing (qid, level) counts as a wrong, empty answer.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident.evaluation.report import build_summary  # noqa: E402
from evident.evaluation import variants  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402
from evident.evaluation.metrics import score  # noqa: E402
from evident.evaluation.schema import Prediction  # noqa: E402


_GOLD = None


def _init_worker(limit):
    global _GOLD
    _GOLD = load_gold(limit)


def _score_task(task):
    pred, qid, lvl = task
    return score(_GOLD[qid], pred, lvl)


def load_predictions(files):
    preds = {}
    for f in files:
        for line in open(f):
            if line.strip():
                p = Prediction.from_json(json.loads(line))
                preds[(p.system, p.qid, p.level)] = p
    return preds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--levels", nargs="+", type=int, default=None,
                    help="default: every level present in the prediction files (absent levels are NOT scored as failures)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-ci", action="store_true")
    ap.add_argument("--workers", type=int, default=1, help="parallel scoring processes (4-8 is a good start)")
    a = ap.parse_args()

    gold = load_gold(a.limit)
    preds = load_predictions(a.predictions)
    systems = sorted({s for s, _, _ in preds})
    if a.levels is None:
        a.levels = sorted({l for _, _, l in preds})
        print(f"levels found in predictions: {a.levels}")
    rows = []
    tasks = [(preds.get((s, qid, lvl)) or Prediction.empty(s, qid, lvl), qid, lvl)
             for s in systems for qid in gold for lvl in a.levels if variants.level_available(qid, lvl)]
    if a.workers > 1:
        # warm the gold-answer cache in THIS process first, so workers only read it (no write races)
        for g in gold.values():
            g.answer_hash()
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker, initargs=(a.limit,)) as ex:
            for i, r in enumerate(ex.map(_score_task, tasks, chunksize=8), 1):
                rows.append(r)
                if i % 400 == 0:
                    print(f"  scored {i}/{len(tasks)}", flush=True)
    else:
        for i, (p, qid, lvl) in enumerate(tasks, 1):
            rows.append(score(gold[qid], p, lvl))
            if i % 400 == 0:
                print(f"  scored {i}/{len(tasks)}", flush=True)
    print(f"scored {len(rows)} predictions for {len(systems)} system(s)", flush=True)
    bad = [(r["system"], r["qid"], r["level"]) for r in rows if r["exec_status"] == "system_error"]
    if bad:
        print(f"\nWARNING: {len(bad)} prediction(s) ended in a FAILED MODEL CALL and are excluded from accuracy (label system_error):")
        for s_, q, l in bad[:10]:
            print(f"   {s_}  {q}  level {l}")
        print("   Re-run the same baseline command with the same --out file: it resumes and retries only the failed calls.\n")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "per_question.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    summary = build_summary(rows, a.levels, len(gold), with_ci=not a.no_ci)
    json.dump(summary, open(out / "summary.json", "w"), indent=1)
    print(f"wrote {out}/summary.json")


if __name__ == "__main__":
    main()
