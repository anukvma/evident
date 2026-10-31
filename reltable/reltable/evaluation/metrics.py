"""Per-prediction metrics. Unavailable inputs give NaN, never 0 (see schema.py)."""
from __future__ import annotations

import math
import sqlite3

from .. import decoys as D
from .. import paths
from ..minidb import build_mini_db
from . import alt_refs, variants
from .gold import Gold, canon_hash, flatten, pair_key, run_sql, set_hash, struct_hash, value_hash
from .schema import Prediction

NAN = float("nan")
KS = (1, 3, 5, 10, 20, 50)


def isnan(x):
    return isinstance(x, float) and math.isnan(x)


def _dedupe_lower(names):
    seen, out = set(), []
    for n in names:
        k = str(n).lower()
        if k not in seen:
            seen.add(k); out.append(k)
    return out


def _table_meta(g: Gold, level: int):
    """lower(name) -> (canonical name, columns, n_rows, is_own) for every candidate table."""
    pool = variants._pool()
    meta = {t.lower(): (t, m["cols"], m["n_rows"], True) for t, m in pool[f"{g.source}/{g.db_id}"].items()}
    for d in variants.manifest(g.qid)["decoys"][:level]:
        m = pool[f"{d['source']}/{d['db_id']}"][d["table"]]
        meta[d["name"].lower()] = (d["name"], m["cols"], min(m["n_rows"], 20000), False)
    return meta


def execute_answer(g: Gold, p: Prediction, level: int):
    """-> (status, exec_correct | None, value_correct, set_correct | None, struct_correct | None).

    exec_correct   strict row-multiset match (SQL results or supplied rows); None when the system gave
                   only flat values, because column layout cannot be judged for a text answer
    value_correct  paradigm-neutral: the flat multiset of normalised cell values matches the gold's,
                   ignoring column layout and row order. Defined for EVERY system, so SQL and non-SQL
                   systems are compared on one scale.
    status: ok | error | timeout | not_select | no_answer."""
    if p.sql:
        head = p.sql.strip().lower().lstrip("(")
        if not (head.startswith("select") or head.startswith("with")):
            return "not_select", False, False, False, False
        con = variants.open_variant(g, level)
        try:
            rows = run_sql(con, p.sql, timeout=60)
        except sqlite3.OperationalError as e:
            return ("timeout" if "interrupted" in str(e) else "error"), False, False, False, False
        except sqlite3.Error:
            return "error", False, False, False, False
        finally:
            con.close()
        return ("ok", canon_hash(rows) == g.answer_hash(), value_hash(flatten(rows)) == g.value_hash(),
                set_hash(rows) == g.set_hash(), struct_hash(rows) == g.struct_hash())
    if p.answer is not None:
        rows = [tuple(r) for r in p.answer]
        return ("ok", canon_hash(rows) == g.answer_hash(), value_hash(flatten(rows)) == g.value_hash(),
                set_hash(rows) == g.set_hash(), struct_hash(rows) == g.struct_hash())
    if p.answer_values is not None:
        return "ok", None, value_hash(list(p.answer_values)) == g.value_hash(), None, None
    return "no_answer", False, False, False, False


def table_metrics(g: Gold, p: Prediction):
    req = _dedupe_lower(g.required_tables)
    m = {}
    if p.ranked_tables is not None:
        ranked = _dedupe_lower(p.ranked_tables)
        for k in KS:
            top = set(ranked[:k])
            hit = sum(1 for t in req if t in top)
            m[f"table_recall@{k}"] = hit / len(req)
            m[f"table_full_coverage@{k}"] = float(hit == len(req))
    else:
        for k in KS:
            m[f"table_recall@{k}"] = m[f"table_full_coverage@{k}"] = NAN
    if p.selected_tables is not None:
        sel = set(_dedupe_lower(p.selected_tables))
        hit = sum(1 for t in req if t in sel)
        m["table_recall_selected"] = hit / len(req)
        m["table_precision_selected"] = (len([t for t in sel if t in req]) / len(sel)) if sel else NAN
        m["n_selected_tables"] = float(len(sel))
    else:
        m["table_recall_selected"] = m["table_precision_selected"] = m["n_selected_tables"] = NAN
    return m


def column_metrics(g: Gold, p: Prediction, level: int):
    if p.selected_columns is None:
        return {"column_recall": NAN, "column_precision": NAN}
    meta = _table_meta(g, level)
    pred = set()
    for t, cols in p.selected_columns.items():
        if t.lower() not in meta:
            continue
        real = meta[t.lower()][1]
        for c in (real if "*" in cols else cols):
            pred.add(f"{t}.{c}".lower())
    gold = {f"{t}.{c}".lower() for t, cs in g.required_columns.items() for c in cs}
    hit = len(pred & gold)
    return {"column_recall": hit / len(gold) if gold else NAN,
            "column_precision": hit / len(pred) if pred else NAN}


def join_metrics(g: Gold, p: Prediction):
    if p.join_keys is None or not g.required_joins:
        return {"join_key_recall": NAN, "join_key_precision": NAN}
    pred = {pair_key(a, b) for a, b in p.join_keys}
    hit = len(pred & g.required_joins)
    return {"join_key_recall": hit / len(g.required_joins),
            "join_key_precision": hit / len(pred) if pred else NAN}


def _pred_ids(g: Gold, p: Prediction, level: int):
    """-> {canonical table: set(rowids) | 'ALL'}, plus total predicted row count (all tables)."""
    meta = _table_meta(g, level)
    ids, total = {}, 0
    for t, v in (p.retrieved_rows or {}).items():
        if t.lower() not in meta:
            continue
        name, _cols, n_rows, _own = meta[t.lower()]
        if isinstance(v, str) and v.upper() == "ALL":
            ids[name] = "ALL"; total += n_rows
        else:
            ids[name] = {int(x) for x in v}; total += len(ids[name])
    return ids, total


def row_metrics(g: Gold, p: Prediction, level: int):
    nanrow = {"row_recall": NAN, "row_precision": NAN, "row_complete_set": NAN,
              "evidence_sufficient": NAN, "evidence_sufficient_eq": NAN, "n_retrieved_rows": NAN}
    if p.retrieved_rows is None:
        return nanrow
    ids, total = _pred_ids(g, p, level)
    gold_total = sum(len(v) for v in g.row_ids.values())
    hit = 0
    for t, gset in g.row_ids.items():
        pr = ids.get(t)
        if pr == "ALL":
            hit += len(gset)
        elif pr:
            hit += len(gset & pr)
    m = {"row_recall": hit / gold_total if gold_total else NAN,
         "row_precision": hit / total if total else NAN,
         "row_complete_set": float(hit == gold_total), "n_retrieved_rows": float(total)}
    # sufficiency by execution: can the gold query still be answered from ONLY the retrieved rows?
    own = {t: v for t, v in ids.items() if t in variants._pool()[f"{g.source}/{g.db_id}"]}
    mini = build_mini_db(g.db_path, list(own), own)
    def reproduces(sql):
        try:
            return canon_hash(run_sql(mini, sql)) == g.answer_hash()
        except sqlite3.Error:
            return False
    try:
        strict = reproduces(g.gold_sql)
        m["evidence_sufficient"] = float(strict)
        # equivalence-aware: also accept a human-CONFIRMED alternative reference query (see alt_refs.py)
        m["evidence_sufficient_eq"] = float(strict or any(reproduces(a) for a in alt_refs.confirmed(g.qid)))
    finally:
        mini.close()
    return m


def cost_metrics(p: Prediction):
    c = p.cost or {}
    keys = ("latency_s", "preprocess_s", "llm_calls", "input_tokens", "output_tokens", "peak_mem_mb")
    return {f"cost_{k}": (float(c[k]) if c.get(k) is not None else NAN) for k in keys}


def score(g: Gold, p: Prediction, level: int) -> dict:
    status, correct, vcorrect, scorrect, tcorrect = execute_answer(g, p, level)
    # A prediction that exists, ranks/selects tables, but never tries to answer is RETRIEVAL-ONLY (e.g. the question-blind
    # control): its answer metrics are not applicable (NaN), not zero. A missing line or an empty prediction is still wrong.
    # A FAILED MODEL CALL (llm_error) is neither wrong nor retrieval-only: it is a system error, excluded from accuracy and
    # reported separately so it can be re-run (silently dropping or silently failing it would bias the comparison).
    errored = bool(p.llm_error) and not p.missing and p.sql is None and p.answer is None and p.answer_values is None
    # retrieval-only = never meant to answer. A system that TRIED (abstained, e.g. "the schemas do not contain this") is wrong.
    retrieval_only = (not p.missing and not errored and not p.abstained and status == "no_answer" and
                      (p.ranked_tables is not None or p.selected_tables is not None))
    no_answer_metrics = retrieval_only or errored
    m = {"qid": g.qid, "system": p.system, "level": level, "source": g.source, "split": g.split,
         "operation": g.operation, "n_tables": min(g.n_tables, 4), "rows_mode": g.rows_mode,
         "db": f"{g.source}/{g.db_id}", "missing_prediction": p.missing,
         "exec_status": "system_error" if errored else ("retrieval_only" if retrieval_only else status),
         "llm_error": (p.llm_error or "")[:120] if errored else "",
         "exec_accuracy": NAN if (correct is None or no_answer_metrics) else float(correct),
         "exec_accuracy_set": NAN if (scorrect is None or no_answer_metrics) else float(scorrect),
         "exec_accuracy_struct": NAN if (tcorrect is None or no_answer_metrics) else float(tcorrect),
         "value_accuracy": NAN if no_answer_metrics else float(vcorrect),
         "answer_correct": NAN if no_answer_metrics else float(vcorrect if correct is None else correct),
         "n_candidate_tables": len(variants.candidate_tables(g, level))}
    m.update(table_metrics(g, p))
    m.update(column_metrics(g, p, level))
    m.update(join_metrics(g, p))
    m.update(row_metrics(g, p, level))
    m.update(cost_metrics(p))
    m["failure"] = attribute(g, p, m)
    return m


def attribute(g: Gold, p: Prediction, m: dict) -> str:
    """First failing stage, in pipeline order. A stage is only checked if the system exposed the
    information for it; otherwise it cannot be blamed. Correct answers return 'correct'."""
    if m["exec_status"] == "system_error":
        return "system_error"
    if isnan(m["answer_correct"]):
        return "retrieval_only"
    if m["answer_correct"] == 1.0:
        return "correct"
    if p.missing or p.abstained or m["exec_status"] == "no_answer":
        return "no_answer"
    retrieval_seen = any(not isnan(m[k]) for k in ("table_recall_selected", "table_recall@10"))
    t_rec = m["table_recall_selected"] if not isnan(m["table_recall_selected"]) else m["table_recall@10"]
    if retrieval_seen and t_rec < 1.0:
        return "missed_table"
    # Columns/joins/rows from the system's own SQL describe what the SQL USED, so a miss there is an
    # SQL-planning failure, not a retrieval one; label it separately so retrieval is not blamed.
    pre = "sql_" if p.evidence_source == "sql_derived" else ""
    if not isnan(m["column_recall"]) and m["column_recall"] < 1.0:
        return pre + "missed_column"
    if not isnan(m["join_key_recall"]) and m["join_key_recall"] < 1.0:
        return pre + "missed_join_key"
    if not isnan(m["row_recall"]) and (m["row_complete_set"] == 0.0 or m["evidence_sufficient"] == 0.0):
        return pre + "incomplete_evidence"
    if m["exec_status"] in ("error", "timeout", "not_select"):
        return "sql_failure"
    return "wrong_answer" if not retrieval_seen else "wrong_answer_despite_evidence"
