"""Retrieve-and-read baseline: NO SQL is written or executed.

  1. table retrieval : BM25 over table documents (name, columns, sample values) -> top-k tables
                       (--table-retriever hybrid: reciprocal-rank fusion of BM25 and dense, as hybrid_t2s)
  2. row retrieval   : BM25 (SQLite FTS5) over the rows of each selected table -> top-R rows
                       (a table with <= R rows is read in full)
                       --row-retriever dense : bge-small over the first DENSE_ROW_CAP rows of the table
                       --row-retriever hybrid: reciprocal-rank fusion of FTS and dense (rows past the cap stay
                                               reachable through FTS)
  3. read            : the LLM answers from those rows only, as a flat JSON list of values

Because it never aggregates in a query engine, it is exactly the family whose limits the benchmark's
complete-set evidence labels expose: aggregation, ranking and negation over more rows than fit in R.
Evidence = the rows placed in the prompt (retrieved_rows), so row recall and evidence sufficiency are
measured on what the model actually saw.

Own tables get a persistent FTS index (built once, build time recorded in data/cache/fts/manifest.json,
reported as preprocessing cost). Decoy tables are small (<= row cap) and indexed in memory per variant.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time

from reltable import paths
from reltable.evaluation import variants
from reltable.llm import LLM

from . import common as C

FTS_DIR = paths.DATA / "cache" / "fts"
DENSE_DIR = paths.DATA / "cache" / "dense_rows"
DENSE_ROW_CAP = 20000            # same cap as decoy tables; embedding millions of rows per table is not feasible on CPU
_BUILD_LOCK = threading.Lock()   # index builds are rare; serialise them (workers share tables)
STOP = set("the a an of in on at to for and or is are was were be been by with from that this which what who whom whose how many much "
           "did does do has have had as it its their there than then list name names show give tell all any each per".split())

PROMPT = """You are answering a question about a database using ONLY the rows shown below. The rows were retrieved by keyword \
search and may be incomplete.

{context}

{knowledge}QUESTION: {question}

Reply with JSON only, in the form {{"answer": [<value>, <value>, ...]}}: a flat list holding every value that answers the \
question (use [] if the rows do not contain the answer). Do not explain."""


def _terms(question, knowledge, cap=40):
    toks = re.findall(r"[A-Za-z0-9]+", f"{question} {knowledge or ''}".lower())
    seen, out = set(), []
    for t in toks:
        if len(t) >= 2 and t not in STOP and t not in seen:
            seen.add(t); out.append(t)
    return out[:cap]


def _row_text(row):
    return " ".join(str(v) for v in row if v is not None)


def _build_fts(src_con, table, dest, cap=None):
    """Contentless FTS5 index: rowid -> row text. Returns seconds spent."""
    t0 = time.time()
    dest.execute("CREATE VIRTUAL TABLE IF NOT EXISTS f USING fts5(txt, content='', tokenize='unicode61')")
    q = f'SELECT rowid, * FROM "{table}"' + (f" ORDER BY rowid LIMIT {cap}" if cap else "")
    cur = src_con.execute(q)
    batch = []
    for r in cur:
        batch.append((r[0], _row_text(r[1:])))
        if len(batch) >= 20000:
            dest.executemany("INSERT INTO f(rowid, txt) VALUES (?, ?)", batch); batch = []
    if batch:
        dest.executemany("INSERT INTO f(rowid, txt) VALUES (?, ?)", batch)
    dest.commit()
    return time.time() - t0


def own_index(g, table):
    """Persistent index on the ORIGINAL table (rowids match the original database)."""
    FTS_DIR.mkdir(parents=True, exist_ok=True)
    f = FTS_DIR / f"{g.source}__{g.db_id}__{table}.sqlite"
    with _BUILD_LOCK:                                   # workers share tables; builds are rare
        if not f.exists():
            tmp = f.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            src = sqlite3.connect(f"file:{g.db_path}?mode=ro", uri=True)
            src.text_factory = lambda b: b.decode("utf-8", "replace")
            dst = sqlite3.connect(tmp)
            secs = _build_fts(src, table, dst)
            n = dst.execute("select count(*) from f_docsize").fetchone()[0]
            dst.close()
            src.close()
            tmp.rename(f)
            mf = FTS_DIR / "manifest.json"
            m = json.load(open(mf)) if mf.exists() else {}
            m[f.name] = {"build_s": round(secs, 3), "rows": n, "bytes": f.stat().st_size}
            json.dump(m, open(mf, "w"))
    return sqlite3.connect(f"file:{f}?mode=ro", uri=True)


def top_rows(index_con, terms, k):
    if not terms:
        return []
    q = " OR ".join(f'"{t}"' for t in terms)
    try:
        return [r[0] for r in index_con.execute(
            "SELECT rowid FROM f WHERE f MATCH ? ORDER BY bm25(f) LIMIT ?", (q, k))]
    except sqlite3.Error:
        return []


def dense_index(src_path, key, table):
    """(rowids, embeddings) for the first DENSE_ROW_CAP rows of a source table, cached on disk by its underlying
    (source, db, table). Returns (ids, matrix, build seconds; 0 when cached)."""
    import numpy as np
    from reltable import embed
    DENSE_DIR.mkdir(parents=True, exist_ok=True)
    f = DENSE_DIR / ("__".join(key) + ".npz")
    with _BUILD_LOCK:
        if f.exists():
            d = np.load(f)
            return d["ids"], d["emb"], 0.0
        t0 = time.time()
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        src.text_factory = lambda b: b.decode("utf-8", "replace")
        cols = [r[1] for r in src.execute(f'pragma table_info("{table}")')]
        rows = src.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid LIMIT {DENSE_ROW_CAP}').fetchall()
        src.close()
        texts = ["; ".join(f"{c}: {v}" for c, v in zip(cols, r[1:]) if v is not None)[:400] for r in rows]
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        emb = embed.encode(texts) if texts else np.zeros((0, 384), dtype=np.float32)
        np.savez(f, ids=ids, emb=emb)
        return ids, emb, time.time() - t0


def dense_top_rows(g, level, name, is_own, k):
    """Top-k rowids (in the variant) by cosine(question, row text). Decoy rows are copied into the variant in rowid
    order, so the i-th row of the source prefix has variant rowid i+1; own tables keep their original rowids."""
    from reltable import embed
    src, db, table = C._origin_of(g, level)[name]
    ids, emb, secs = dense_index(paths.sqlite_path(src, db), (src, db, table), table)
    if len(ids) == 0:
        return [], secs
    q = embed.encode([f"{g.question} {g.knowledge or ''}".strip()], query=True)[0]
    order = (emb @ q).argsort()[::-1][:k]
    return ([int(ids[i]) for i in order] if is_own else [int(i) + 1 for i in order]), secs


def fetch_rows(con, table, rowids, max_chars=80):
    if not rowids:
        return [], []
    cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
    ph = ",".join("?" * len(rowids))
    rows = con.execute(f'SELECT rowid, * FROM "{table}" WHERE rowid IN ({ph}) ORDER BY rowid', list(rowids)).fetchall()
    cut = lambda v: "" if v is None else (str(v)[:max_chars] + "…" if len(str(v)) > max_chars else str(v))  # noqa: E731
    return cols, [(r[0], [cut(v) for v in r[1:]]) for r in rows]


def md_table(name, cols, rows):
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
    body = "\n".join("| " + " | ".join(v) + " |" for _rid, v in rows)
    return f"### {name}\n{head}\n{body}"


def parse_answer(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            ans = d.get("answer", d) if isinstance(d, dict) else d
            if not isinstance(ans, list):
                ans = [ans]
            flat = []
            for a in ans:
                flat += a if isinstance(a, list) else [a]
            return flat
        except (json.JSONDecodeError, AttributeError):
            pass
    t = (text or "").strip()
    return [t] if t else None


def system_name(table_retriever="bm25", row_retriever="fts", read_tables=3, rows_per_table=30):
    """The pilot configuration keeps its original name, so its cached predictions and scores stay valid."""
    if (table_retriever, row_retriever, read_tables, rows_per_table) == ("bm25", "fts", 3, 30):
        return "retrieve_read"
    return f"retrieve_read-{table_retriever}-{row_retriever}-{read_tables}x{rows_per_table}"


def predict(g, level, llm: LLM, read_tables=3, rows_per_table=30, dry_run=False, table_retriever="bm25",
            row_retriever="fts", **_):
    t0 = time.time()
    con = variants.open_variant(g, level)
    preprocess_s = 0.0
    try:
        pm = C._table_meta(g, level)
        cands = [pm[k][0] for k in pm]
        docs = {t: C.table_doc(con, t, pm[t.lower()][1]) for t in cands}
        ranked = C.bm25_rank(g.question, g.knowledge, docs)
        if table_retriever == "hybrid":
            ranked = C.rrf([ranked, C.dense_rank(g, level, con, cands)])[1]
        chosen = ranked[:read_tables]
        terms = _terms(g.question, g.knowledge)
        parts, retrieved, n_total = [], {}, 0
        for t in chosen:
            name, cols_, n_rows, is_own = pm[t.lower()]
            if n_rows <= rows_per_table:
                ids = [r[0] for r in con.execute(f'SELECT rowid FROM "{name}" ORDER BY rowid LIMIT {rows_per_table}')]
            else:
                ids = []
                if row_retriever in ("fts", "hybrid"):
                    if is_own:
                        ix = own_index(g, name)
                        ids = top_rows(ix, terms, rows_per_table); ix.close()
                    else:                                   # decoy: small, index in memory from the variant
                        mem = sqlite3.connect(":memory:")
                        _build_fts(con, name, mem)
                        ids = top_rows(mem, terms, rows_per_table); mem.close()
                if row_retriever in ("dense", "hybrid"):
                    dids, secs = dense_top_rows(g, level, name, is_own, rows_per_table)
                    preprocess_s += secs
                    ids = dids if row_retriever == "dense" else C.rrf([ids, dids])[1][:rows_per_table]
            if not ids:                                     # no keyword hit: show the head of the table, as a reader would
                ids = [r[0] for r in con.execute(f'SELECT rowid FROM "{name}" ORDER BY rowid LIMIT {rows_per_table}')]
            cols, rows = fetch_rows(con, name, ids)
            retrieved[name] = [r[0] for r in rows]
            n_total += len(rows)
            parts.append(md_table(f"{name}  ({n_rows} rows in table, {len(rows)} shown)", cols, rows))
        kn = f"EXTERNAL KNOWLEDGE: {g.knowledge}\n\n" if g.knowledge else ""
        prompt = PROMPT.format(context="\n\n".join(parts), knowledge=kn, question=g.question)
        retrieval_s = time.time() - t0
        rec = {"system": f"{system_name(table_retriever, row_retriever, read_tables, rows_per_table)}[{llm.model}]", "qid": g.qid, "level": level, "ranked_tables": ranked,
               "selected_tables": chosen, "selected_columns": {t: ["*"] for t in chosen},
               "retrieved_rows": retrieved, "evidence_source": "context_rows", "prompt_chars": len(prompt),
               "truncated_tables": []}
        if dry_run:
            return rec
        meta = {}
        if llm.is_mock:                                     # test double needs the answer
            gc = variants.open_variant(g, 0)
            meta["gold_values"] = [c for r in gc.execute(g.gold_sql).fetchall() for c in r]
            gc.close()
        r = llm.complete(prompt, meta=meta)
        vals = parse_answer(r["text"])
        rec.update({"answer_values": vals, "raw_response": r["text"][:2000], "llm_error": r["error"],
                    "abstained": vals is None})
        rec["cost"] = {"latency_s": retrieval_s + r["latency_s"], "llm_calls": 1, "input_tokens": r["input_tokens"],
                       "output_tokens": r["output_tokens"], "tokens_estimated": r.get("tokens_estimated", True), "tokenizer": r.get("tokenizer", "chars/4"), "cached": r["cached"],
                       "preprocess_s": preprocess_s, "rows_in_prompt": n_total}
        return rec
    finally:
        con.close()
