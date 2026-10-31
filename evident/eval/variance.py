"""Run-to-run variance of an LLM system from repeated runs. NO scoring happens here.

Repeats are produced with   python baselines/run_baseline.py ... --no-cache --run-id r2 --out data/predictions/<x>.r2.jsonl
and scored together with the original run (system names  'bm25_t2s[model]', 'bm25_t2s[model]#r2', ...).

  python eval/variance.py --per-question reports/eval_variance/per_question.jsonl [--metric value_accuracy]

For each base system: per-run mean, then mean / sd / min-max ACROSS runs, overall and per decoy level; and the question-level
flip rate (share of questions whose correctness differs between at least two runs). A scale effect smaller than the
between-run spread should not be reported as an effect.
"""
import argparse
import collections
import json
import statistics as st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-question", required=True)
    ap.add_argument("--metric", default="value_accuracy")
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.per_question)]
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        base, _, run = r["system"].partition("#")
        groups[base][run or "r1"].append(r)
    m = a.metric
    for base, runs in groups.items():
        if len(runs) < 2:
            print(f"{base}: only one run ({list(runs)}); nothing to compare")
            continue
        names = sorted(runs)
        levels = sorted({r["level"] for r in runs[names[0]]})
        print(f"\n== {base}   runs: {names}   metric: {m}")
        def runmean(rs, lvl=None):
            v = [x[m] for x in rs if (lvl is None or x["level"] == lvl) and x[m] == x[m]]
            return sum(v) / len(v) if v else float("nan")
        means = [runmean(runs[n]) for n in names]
        print(f"  overall   per-run {['%.3f' % x for x in means]}  mean {st.mean(means):.3f}  sd {st.stdev(means):.3f}  "
              f"range {max(means) - min(means):.3f}")
        for lvl in levels:
            lm = [runmean(runs[n], lvl) for n in names]
            print(f"  level {lvl:>3} per-run {['%.3f' % x for x in lm]}  sd {st.stdev(lm):.3f}  range {max(lm) - min(lm):.3f}")
        by_q = collections.defaultdict(set)
        for n in names:
            for r in runs[n]:
                by_q[(r["qid"], r["level"])].add(r[m])
        flips = sum(len(v) > 1 for v in by_q.values())
        print(f"  prediction-level flips between runs: {flips}/{len(by_q)} ({flips / len(by_q):.1%})")


if __name__ == "__main__":
    main()
