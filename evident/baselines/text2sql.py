"""Schema-grounded text-to-SQL baselines (one LLM call per question x level).

  oracle_schema_t2s  gives the LLM exactly the gold tables       (upper bound: isolates reasoning from retrieval)
  bm25_t2s           BM25 over table documents -> top-k tables    (lexical schema retrieval)
  fullschema_t2s     every candidate table's schema in the prompt  (full-context analogue; budget-capped)
  dense_t2s          bge-small embeddings over the same table documents -> top-k   (semantic schema retrieval)
  hybrid_t2s         reciprocal-rank fusion of BM25 and dense -> top-k
  graph_t2s          personalized PageRank over the FK graph seeded by the hybrid scores -> top-k (graph-aware)
All retrieval systems show the same number of tables (top-k), so they differ only in which tables they choose.

Evidence for the evidence/row metrics is SQL-derived (see common.derive_evidence).
"""
from __future__ import annotations

import time

from evident.evaluation import variants
from evident.llm import LLM

from . import common as C

SYSTEMS = ("oracle_schema_t2s", "bm25_t2s", "fullschema_t2s", "dense_t2s", "hybrid_t2s", "graph_t2s")


def build_context(system, g, level, con, topk=5, n_sample_rows=3, max_prompt_chars=300_000):
    """-> (tables_in_prompt, ranked_tables or None, schema_text, truncated_tables)"""
    meta = {t.lower(): t for t in variants.candidate_tables(g, level)}
    cands = list(meta.values())
    ranked = None
    if system == "oracle_schema_t2s":
        tables = list(g.required_tables)
    elif system == "bm25_t2s":
        pm = C._table_meta(g, level)
        docs = {t: C.table_doc(con, t, pm[t.lower()][1]) for t in cands}
        ranked = C.bm25_rank(g.question, g.knowledge, docs)
        tables = ranked[:topk]
    elif system in ("dense_t2s", "hybrid_t2s", "graph_t2s"):
        pm = C._table_meta(g, level)
        docs = {t: C.table_doc(con, t, pm[t.lower()][1]) for t in cands}
        if system == "dense_t2s":
            ranked = C.dense_rank(g, level, con, cands)
        elif system == "hybrid_t2s":
            ranked = C.rrf([C.bm25_rank(g.question, g.knowledge, docs), C.dense_rank(g, level, con, cands)])[1]
        else:
            ranked = C.graph_rank(g, level, con, cands, docs)
        tables = ranked[:topk]
    elif system == "fullschema_t2s":
        tables = cands                                   # candidate order = own tables, then decoys
    else:
        raise ValueError(system)
    truncated = []
    schema = C.schema_block(g, level, tables, con, n_sample_rows)
    if len(schema) > max_prompt_chars:                   # 1) drop sample rows, 2) drop trailing tables
        schema = C.schema_block(g, level, tables, con, 0)
        while len(schema) > max_prompt_chars and len(tables) > 1:
            truncated.append(tables.pop())
            schema = C.schema_block(g, level, tables, con, 0)
    return tables, ranked, schema, truncated


def predict(system, g, level, llm: LLM, topk=5, n_sample_rows=3, max_prompt_chars=300_000,
            derive=True, dry_run=False):
    t0 = time.time()
    con = variants.open_variant(g, level)
    try:
        tables, ranked, schema, truncated = build_context(system, g, level, con, topk, n_sample_rows,
                                                          max_prompt_chars)
        prompt = C.build_prompt(g.question, g.knowledge, schema)
        retrieval_s = time.time() - t0
        rec = {"system": f"{system}[{llm.model}]", "qid": g.qid, "level": level,
               "selected_tables": tables, "truncated_tables": truncated,
               "prompt_chars": len(prompt)}
        if ranked is not None:
            rec["ranked_tables"] = ranked
        if dry_run:
            return rec
        r = llm.complete(prompt, meta={"gold_sql": g.gold_sql})
        sql = C.extract_sql(r["text"])
        rec.update({"sql": sql, "raw_response": r["text"][:4000], "llm_error": r["error"],
                    "abstained": sql is None})
        if derive and sql:
            ev = C.derive_evidence(g, level, sql, con)
            # keep the prompt-level selection (what the model SAW); evidence tables are what its SQL READ
            ev["read_tables"] = ev.pop("selected_tables", None)
            rec.update(ev)
        rec["cost"] = {"latency_s": retrieval_s + r["latency_s"], "llm_calls": 1,
                       "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                       "tokens_estimated": r.get("tokens_estimated", True), "tokenizer": r.get("tokenizer", "chars/4"), "cached": r["cached"], "preprocess_s": 0.0}
        return rec
    finally:
        con.close()
