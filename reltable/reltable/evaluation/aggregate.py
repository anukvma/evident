"""Aggregation: group means, cluster-bootstrap CIs, scale degradation, paired comparison.
Questions from one database are correlated, so bootstrap resamples DATABASES, not questions."""
from __future__ import annotations

import math
import random

import numpy as np

METRICS = ["exec_accuracy", "exec_accuracy_set", "exec_accuracy_struct", "value_accuracy", "table_recall@1", "table_recall@5", "table_recall@10", "table_recall@50",
           "table_full_coverage@10", "table_recall_selected", "table_precision_selected",
           "column_recall", "column_precision", "join_key_recall", "join_key_precision",
           "row_recall", "row_precision", "row_complete_set", "evidence_sufficient", "evidence_sufficient_eq",
           "cost_latency_s", "cost_input_tokens", "cost_output_tokens", "cost_llm_calls"]
GROUPS = ["level", "source", "operation", "n_tables", "rows_mode", "split"]


def _vals(rows, metric):
    return [r[metric] for r in rows if not (isinstance(r[metric], float) and math.isnan(r[metric]))]


def mean_ci(rows, metric, B=1000, seed=0):
    """-> {mean, lo, hi, n} ; mean over rows where the metric is available; CI by database bootstrap."""
    v = _vals(rows, metric)
    if not v:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    by_db = {}
    for r in rows:
        x = r[metric]
        if not (isinstance(x, float) and math.isnan(x)):
            by_db.setdefault(r["db"], []).append(x)
    dbs = list(by_db)
    rng = random.Random(seed)
    boots = []
    for _ in range(B):
        samp = [x for d in (rng.choice(dbs) for _ in dbs) for x in by_db[d]]
        boots.append(sum(samp) / len(samp))
    return {"mean": float(np.mean(v)), "lo": float(np.percentile(boots, 2.5)),
            "hi": float(np.percentile(boots, 97.5)), "n": len(v)}


def summarise(rows, metrics=METRICS, with_ci=True):
    out = {}
    for m in metrics:
        if with_ci:
            out[m] = mean_ci(rows, m)
        else:
            v = _vals(rows, m)
            out[m] = {"mean": float(np.mean(v)) if v else None, "n": len(v)}
    return out


def by_group(rows, key, metrics=METRICS):
    groups = {}
    for r in rows:
        groups.setdefault(str(r[key]), []).append(r)
    return {k: summarise(v, metrics, with_ci=False) for k, v in sorted(groups.items())}


def scale_degradation(rows, metrics=("value_accuracy", "exec_accuracy", "table_recall@10", "table_recall_selected",
                                     "row_recall", "evidence_sufficient"), B=1000, seed=0):
    """Delta M(level) = M(level) - M(0), paired by question, CI by database bootstrap."""
    base = {r["qid"]: r for r in rows if r["level"] == 0}
    out = {}
    for lvl in sorted({r["level"] for r in rows if r["level"] != 0}):
        out[str(lvl)] = {}
        for m in metrics:
            diffs = []
            for r in rows:
                if r["level"] != lvl or r["qid"] not in base:
                    continue
                a, b = r[m], base[r["qid"]][m]
                if isinstance(a, float) and isinstance(b, float) and not (math.isnan(a) or math.isnan(b)):
                    diffs.append({"db": r["db"], m: a - b})
            out[str(lvl)][m] = mean_ci(diffs, m, B, seed) if diffs else {"mean": None, "n": 0}
    return out


def paired_difference(rows_a, rows_b, metric, B=1000, seed=0):
    """Mean of (A - B) over matched (qid, level); CI by database bootstrap."""
    bidx = {(r["qid"], r["level"]): r for r in rows_b}
    diffs = []
    for r in rows_a:
        o = bidx.get((r["qid"], r["level"]))
        if o is None:
            continue
        a, b = r[metric], o[metric]
        if not (math.isnan(a) or math.isnan(b)):
            diffs.append({"db": r["db"], metric: a - b})
    return mean_ci(diffs, metric, B, seed) if diffs else {"mean": None, "n": 0}


def failure_breakdown(rows):
    from collections import Counter
    c = Counter(r["failure"] for r in rows)
    n = sum(c.values())
    return {k: {"n": v, "share": round(v / n, 4)} for k, v in c.most_common()}


def cost_summary(rows):
    def q(v, p):
        return float(np.percentile(v, p)) if v else None
    lat = _vals(rows, "cost_latency_s")
    return {"latency_s_median": q(lat, 50), "latency_s_p95": q(lat, 95),
            "preprocess_s_total": float(sum(_vals(rows, "cost_preprocess_s"))) if _vals(rows, "cost_preprocess_s") else None,
            "input_tokens_mean": float(np.mean(_vals(rows, "cost_input_tokens"))) if _vals(rows, "cost_input_tokens") else None,
            "output_tokens_mean": float(np.mean(_vals(rows, "cost_output_tokens"))) if _vals(rows, "cost_output_tokens") else None,
            "llm_calls_mean": float(np.mean(_vals(rows, "cost_llm_calls"))) if _vals(rows, "cost_llm_calls") else None,
            "peak_mem_mb_max": max(_vals(rows, "cost_peak_mem_mb")) if _vals(rows, "cost_peak_mem_mb") else None}
