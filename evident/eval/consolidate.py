"""Merge per-system scoring outputs into one summary and print the headline tables. NO scoring happens here.

  # score each system in its own terminal (parallel), each writing its own folder:
  python eval/run_eval.py --predictions data/predictions/<system>.jsonl --out reports/eval_parts/<name>
  # then merge:
  python eval/consolidate.py --parts reports/eval_parts/* --out reports/eval_final
  # or just re-summarise one existing run:
  python eval/consolidate.py --parts reports/eval_pilot_v1 --out reports/eval_final

Writes <out>/per_question.jsonl, <out>/summary.json and <out>/headline.md (copy-pasteable tables).
Refuses to merge parts whose levels or question sets differ, or that score the same system twice.
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident.evaluation.report import build_summary  # noqa: E402


def fmt(x, nd=3):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def cell(d, ci=True):
    if d is None or d.get("mean") is None:
        return "n/a"
    s = f"{d['mean']:.3f}"
    if ci and d.get("lo") is not None:
        s += f" [{d['lo']:.2f},{d['hi']:.2f}]"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-ci", action="store_true")
    a = ap.parse_args()

    rows, seen = [], {}
    for p in a.parts:
        f = Path(p) / "per_question.jsonl"
        if not f.exists():
            sys.exit(f"missing {f}")
        part = [json.loads(l) for l in open(f) if l.strip()]
        need = {"value_accuracy", "answer_correct", "exec_accuracy", "failure", "db", "level", "system"}
        missing = need - set(part[0])
        if missing:
            sys.exit(f"{f} was scored with an older harness (missing columns {sorted(missing)}). "
                     f"Re-run eval/run_eval.py for that system; do not mix scoring versions.")
        for s in {r["system"] for r in part}:
            if s in seen:
                sys.exit(f"system '{s}' appears in both {seen[s]} and {p}; refusing to double count")
            seen[s] = p
        rows += part
    levels = sorted({r["level"] for r in rows})
    qsets = {s: {r["qid"] for r in rows if r["system"] == s} for s in seen}
    sizes = {len(v) for v in qsets.values()}
    if len(sizes) != 1 or len({frozenset(v) for v in qsets.values()}) != 1:
        sys.exit(f"systems were scored on different question sets: { {s: len(v) for s, v in qsets.items()} }")
    per_sys_levels = {s: sorted({r['level'] for r in rows if r['system'] == s}) for s in seen}
    if any(v != levels for v in per_sys_levels.values()):
        sys.exit(f"systems cover different levels: {per_sys_levels}")
    n_q = sizes.pop()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "per_question.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    summ = build_summary(rows, levels, n_q, with_ci=not a.no_ci)
    json.dump(summ, open(out / "summary.json", "w"), indent=1)

    S = summ["systems"]
    errs = {s: sum(r["exec_status"] == "system_error" for r in rows if r["system"] == s) for s in S}
    if any(errs.values()):
        print("WARNING: failed model calls excluded from accuracy:", {k: v for k, v in errs.items() if v})
    md = [f"# Headline results ({n_q} questions, levels {levels})\n",
          "Mean over questions; [95% CI] by database bootstrap. `n/a` = metric unavailable for that system.\n"]
    md.append("## Overall\n\n| System | value_acc | exec_acc | exec_acc_set | table_recall@10 | table_recall_selected | column_recall | row_recall | evid_suff | evid_suff_eq |\n|---|---|---|---|---|---|---|---|---|---|")
    for s, v in S.items():
        o = v["overall"]
        md.append(f"| {s} | " + " | ".join(cell(o[m], not a.no_ci) for m in
                  ("value_accuracy", "exec_accuracy", "exec_accuracy_set", "table_recall@10", "table_recall_selected",
                   "column_recall", "row_recall", "evidence_sufficient", "evidence_sufficient_eq")) + " |")
    for metric, title in (("value_accuracy", "value_accuracy by decoy level"), ("exec_accuracy", "exec_accuracy by decoy level"),
                          ("table_recall@10", "table_recall@10 by decoy level"),
                          ("evidence_sufficient", "evidence_sufficient by decoy level")):
        md.append(f"\n## {title}\n\n| System | " + " | ".join(f"L{l}" for l in levels) + " |\n|---|" + "---|" * len(levels))
        for s, v in S.items():
            md.append(f"| {s} | " + " | ".join(fmt(v["by_level"][str(l)][metric]["mean"]) for l in levels) + " |")
    md.append("\n## Scale degradation vs level 0 (paired by question)\n\n| System | Δ at level | value_acc | table_recall@10 | evidence_sufficient |\n|---|---|---|---|---|")
    for s, v in S.items():
        sd = v.get("scale_degradation_vs_level0") or {}
        for l, d in sd.items():
            md.append(f"| {s} | L{l} | " + " | ".join(cell(d.get(m), True) for m in ("value_accuracy", "table_recall@10", "evidence_sufficient")) + " |")
    md.append("\n## Failure breakdown (share of all 800 predictions)\n")
    for s, v in S.items():
        md.append(f"- **{s}**: " + ", ".join(f"{k} {x['share']:.1%}" for k, x in v["failure_breakdown"].items()))
    md.append("\n## By operation (value_accuracy)\n\n| System | " + " | ".join(sorted(S[next(iter(S))]['by_operation'])) + " |\n|---|" + "---|" * len(S[next(iter(S))]['by_operation']))
    for s, v in S.items():
        md.append(f"| {s} | " + " | ".join(fmt(v["by_operation"][k]["value_accuracy"]["mean"]) for k in sorted(v["by_operation"])) + " |")
    md.append("\n## Cost\n\n| System | median latency (s) | p95 latency (s) | mean input tokens | mean output tokens |\n|---|---|---|---|---|")
    for s, v in S.items():
        c = v["cost"]
        md.append(f"| {s} | {fmt(c['latency_s_median'], 2)} | {fmt(c['latency_s_p95'], 2)} | {fmt(c['input_tokens_mean'], 0)} | {fmt(c['output_tokens_mean'], 0)} |")
    (out / "headline.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nwrote {out}/summary.json, per_question.jsonl, headline.md")


if __name__ == "__main__":
    main()
