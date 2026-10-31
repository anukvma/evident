"""Shared pieces for baselines: schema serialisation, BM25 table retrieval, SQL extraction and
SQL-derived evidence."""
from __future__ import annotations

import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import decoys as D  # noqa: E402
from evident.evaluation import variants  # noqa: E402
from evident.evaluation.gold import run_sql  # noqa: E402
from evident.evaluation.metrics import _table_meta  # noqa: E402
from evident import sqlast  # noqa: E402


# ---------------------------------------------------------------- schema serialisation
def uniform_ddl(g, level, name, meta, name_map):
    """Same DDL generator for own AND decoy tables (PRAGMA-based, FKs only to tables in the variant),
    so the prompt cannot reveal which tables are real through DDL style."""
    pool = variants._pool()
    src_key = f"{g.source}/{g.db_id}"
    info = {"source": g.source, "db_id": g.db_id, "table": meta[name.lower()][0], "name": meta[name.lower()][0]}
    real = [d for d in variants.manifest(g.qid)["decoys"][:level] if d["name"].lower() == name.lower()]
    if real:
        info = real[0]
    return D.decoy_ddl(pool, info, name_map)


def name_map_for(g, level):
    pool = variants._pool()
    nm = {(g.source, g.db_id, t): t for t in pool[f"{g.source}/{g.db_id}"]}
    for d in variants.manifest(g.qid)["decoys"][:level]:
        nm[(d["source"], d["db_id"], d["table"])] = d["name"]
    return nm


def sample_rows(con, table, n, max_chars=40):
    if n <= 0:
        return []
    try:
        rows = con.execute(f'SELECT * FROM "{table}" LIMIT {n}').fetchall()
    except sqlite3.Error:
        return []
    cut = lambda v: (str(v)[:max_chars] + "…") if v is not None and len(str(v)) > max_chars else v  # noqa: E731
    return [tuple(cut(v) for v in r) for r in rows]


def schema_block(g, level, tables, con, n_sample_rows=3):
    meta = _table_meta(g, level)
    nm = name_map_for(g, level)
    parts = []
    for t in tables:
        ddl = uniform_ddl(g, level, t, meta, nm)
        rows = sample_rows(con, meta[t.lower()][0], n_sample_rows)
        txt = ddl
        if rows:
            txt += "\n-- sample rows: " + " | ".join(str(r) for r in rows)
        parts.append(txt)
    return "\n\n".join(parts)


SQL_PROMPT = """You are an expert SQLite analyst. Write ONE SQLite query that answers the question using only the tables shown.

{schema}

{knowledge}QUESTION: {question}

Rules: output only the SQL inside a ```sql fenced block. Quote identifiers that contain spaces or special characters with double quotes. Do not invent tables or columns."""


def build_prompt(question, knowledge, schema):
    kn = f"EXTERNAL KNOWLEDGE (use it to interpret the question): {knowledge}\n\n" if knowledge else ""
    return SQL_PROMPT.format(schema=schema, knowledge=kn, question=question)


_FENCE = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text: str) -> str | None:
    m = _FENCE.search(text or "")
    cand = m.group(1) if m else (text or "")
    cand = cand.strip().rstrip(";").strip()
    if re.match(r"(?is)^\s*(select|with)\b", cand):
        return cand
    m2 = re.search(r"(?is)\b(select|with)\b.*", cand)
    return m2.group(0).strip().rstrip(";") if m2 else None


# ---------------------------------------------------------------- BM25 over table documents
def _tok(s: str):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(s))
    return [w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if w]


def table_doc(con, name, cols, n_rows_for_values=3):
    toks = _tok(name) * 2
    for c in cols:
        toks += _tok(c)
    for r in sample_rows(con, name, n_rows_for_values, max_chars=60):
        for v in r:
            if isinstance(v, str):
                toks += _tok(v)
    return toks


def bm25_rank(question, knowledge, docs: dict, k1=1.5, b=0.75):
    """docs: {table: tokens}. -> [table, ...] best first. Deterministic tie-break by name."""
    N = len(docs)
    avgdl = sum(len(d) for d in docs.values()) / max(N, 1)
    df = Counter()
    for d in docs.values():
        df.update(set(d))
    q = _tok(question) + _tok(knowledge or "")
    scores = {}
    for t, d in docs.items():
        tf = Counter(d)
        s = 0.0
        for w in set(q):
            if w in tf:
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avgdl))
        scores[t] = s
    return sorted(docs, key=lambda t: (-scores[t], t.lower()))


# ---------------------------------------------------------------- SQL-derived evidence
def derive_evidence(g, level, sql, con, row_timeout=60):
    """Evidence implied by the system's own SQL: tables/columns it reads, joins it equates, and the rows that
    pass its joins/filters (our evidence rewrite applied to the PREDICTED sql). Labelled as sql_derived.
    Returns {} when the SQL cannot be analysed (metrics then stay NaN)."""
    meta = _table_meta(g, level)
    schema = {meta[k][0]: {c: "TEXT" for c in meta[k][1]} for k in meta}
    an = sqlast.Analyzer(schema).analyze(sql)
    if not an.get("parse_ok"):
        return {"evidence_error": an.get("parse_error")}
    rows: dict[str, set] = {}
    err = None
    for st in an["steps"]:
        if not st["evidence_sql"]:
            continue
        try:
            for row in run_sql(con, st["evidence_sql"], timeout=row_timeout):
                for tname, rid in zip(st["evidence_tables"], row):
                    if rid is not None:
                        rows.setdefault(tname, set()).add(rid)
        except sqlite3.Error as e:
            err = str(e)[:100]
    joins = [list(j["pair"]) for j in an["join_keys"] if len(j["pair"]) == 2]
    joins += [[j["outer"], j["inner"]] for j in an["implicit_joins"] if j["outer"] and j["inner"]]
    out = {"selected_tables": sorted(an["reads"]),
           "selected_columns": {t: list(c) for t, c in an["reads"].items()},
           "join_keys": joins, "retrieved_rows": {t: sorted(v) for t, v in rows.items()},
           "evidence_source": "sql_derived"}
    if err:
        out["evidence_error"] = err
    return out


# ---------------------------------------------------------------- dense, hybrid and graph-aware table retrieval
_EMB_CACHE: dict = {}          # (source, db, table) -> embedding; a decoy's text is the same in every variant


def _origin_of(g, level):
    """variant table name -> (source, db, table) of the underlying table."""
    pool = variants._pool()
    out = {t: (g.source, g.db_id, t) for t in pool[f"{g.source}/{g.db_id}"]}
    for d in variants.manifest(g.qid)["decoys"][:level]:
        out[d["name"]] = (d["source"], d["db_id"], d["table"])
    return out


def dense_doc(con, name, cols, n_rows_for_values=3):
    """Passage for dense retrieval: the same information BM25 sees (name, columns, sample string values)."""
    from evident import embed
    vals = [v for r in sample_rows(con, name, n_rows_for_values, max_chars=60) for v in r if isinstance(v, str)]
    return embed.schema_text(name, cols) + (("; values: " + ", ".join(vals)) if vals else "")


def dense_scores(g, level, con, cands):
    """{table: cosine(question, table passage)} with bge-small; passages cached by underlying table."""
    import numpy as np
    from evident import embed
    meta, orig = _table_meta(g, level), _origin_of(g, level)
    missing = [t for t in cands if orig[t] not in _EMB_CACHE]
    if missing:
        vecs = embed.encode([dense_doc(con, t, meta[t.lower()][1]) for t in missing])
        for t, v in zip(missing, vecs):
            _EMB_CACHE[orig[t]] = v
    q = embed.encode([f"{g.question} {g.knowledge or ''}".strip()], query=True)[0]
    M = np.stack([_EMB_CACHE[orig[t]] for t in cands])
    return dict(zip(cands, (M @ q).tolist()))


def dense_rank(g, level, con, cands):
    s = dense_scores(g, level, con, cands)
    return sorted(cands, key=lambda t: (-s[t], t.lower()))


def rrf(rankings, k=60):
    """Reciprocal-rank fusion -> ({table: score}, ranking best first)."""
    score: dict = {}
    for ranking in rankings:
        for i, t in enumerate(ranking):
            score[t] = score.get(t, 0.0) + 1.0 / (k + i + 1)
    return score, sorted(score, key=lambda t: (-score[t], str(t).lower()))


def fk_graph(g, level):
    """Undirected FK adjacency among the variant's tables (own FKs + decoy FKs whose target is in the variant)."""
    pool = variants._pool()
    adj: dict = {t: set() for t in pool[f"{g.source}/{g.db_id}"]}
    for t, m in pool[f"{g.source}/{g.db_id}"].items():
        for (_f, ref, _to) in m["fks"]:
            for r in adj:
                if r.lower() == str(ref).lower() and r != t:
                    adj[t].add(r); adj[r].add(t)
    decoys = variants.manifest(g.qid)["decoys"][:level]
    name_of = {(d["source"], d["db_id"], d["table"]): d["name"] for d in decoys}
    for d in decoys:
        adj.setdefault(d["name"], set())
        for (_f, ref, _to) in pool[f"{d['source']}/{d['db_id']}"][d["table"]]["fks"]:
            r = name_of.get((d["source"], d["db_id"], ref))
            if r and r != d["name"]:
                adj[d["name"]].add(r); adj.setdefault(r, set()).add(d["name"])
    return adj


def graph_rank(g, level, con, cands, docs, alpha=0.5, iters=30, n_seeds=10):
    """Personalized PageRank over the FK graph, restarting at the hybrid (BM25 + dense) top-n_seeds with weights
    proportional to their fused scores. Tables joined to strong matches rise; ties fall back to the hybrid order."""
    fused, hyb = rrf([bm25_rank(g.question, g.knowledge, docs), dense_rank(g, level, con, cands)])
    seeds = hyb[:n_seeds]
    z = sum(fused[t] for t in seeds)
    restart = {t: fused[t] / z for t in seeds}
    adj = fk_graph(g, level)
    pr = dict(restart)
    for _ in range(iters):
        nxt = {t: (1 - alpha) * restart.get(t, 0.0) for t in cands}
        for t, v in pr.items():
            nb = [n for n in adj.get(t, ()) if n in nxt]
            if nb:
                for n in nb:
                    nxt[n] += alpha * v / len(nb)
            else:
                nxt[t] += alpha * v                         # dangling node keeps its mass
        pr = nxt
    order = {t: i for i, t in enumerate(hyb)}
    return sorted(cands, key=lambda t: (-pr.get(t, 0.0), order.get(t, len(order))))
