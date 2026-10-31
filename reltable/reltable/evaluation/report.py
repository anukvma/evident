"""Build the summary document from per-question rows (shared by eval/run_eval.py and eval/consolidate.py)."""
from __future__ import annotations

from . import aggregate as A


def build_summary(rows, levels, n_questions, with_ci=True):
    systems = sorted({r["system"] for r in rows})
    summary = {"n_questions": n_questions, "levels": levels, "systems": {}}
    for s in systems:
        mine = [r for r in rows if r["system"] == s]
        summary["systems"][s] = {
            "coverage": {"predictions_present": sum(not r["missing_prediction"] for r in mine),
                         "expected": len(mine)},
            "overall": A.summarise(mine, with_ci=with_ci),
            "by_level": A.by_group(mine, "level"),
            "by_source": A.by_group(mine, "source"),
            "by_operation": A.by_group(mine, "operation"),
            "by_n_tables": A.by_group(mine, "n_tables"),
            "by_rows_mode": A.by_group(mine, "rows_mode"),
            "scale_degradation_vs_level0": A.scale_degradation(mine) if 0 in levels else None,
            "failure_breakdown": A.failure_breakdown(mine),
            "failure_breakdown_by_level": {str(l): A.failure_breakdown([r for r in mine if r["level"] == l])
                                           for l in levels},
            "cost": A.cost_summary(mine),
        }
    if len(systems) > 1:
        base = systems[0]
        summary["paired_vs_" + base] = {
            s: {m: A.paired_difference([r for r in rows if r["system"] == s],
                                       [r for r in rows if r["system"] == base], m)
                for m in ("exec_accuracy", "value_accuracy", "table_recall@10", "row_recall")}
            for s in systems[1:]}
    return summary
