"""Gold records for the pilot: question, gold SQL/answer hash, required evidence, row ids."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field

from .. import paths
from ..minidb import lenient

CACHE = paths.active()["cache"]
TIMEOUT_S = 120


def norm_cell(x):
    """Cell normalisation for answer comparison: numbers compare numerically to 6 significant
    digits (so 5 == 5.0 and float noise is ignored), bytes decode, NULL stays NULL."""
    if x is None:
        return None
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(f"{float(x):.6g}")
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return x


def canon_hash(rows) -> str:
    """Order-insensitive multiset hash of result rows (duplicates matter, order does not)."""
    keyed = sorted(repr(tuple(norm_cell(c) for c in r)) for r in rows)
    return hashlib.sha1("\n".join(keyed).encode()).hexdigest()


def norm_value(x):
    """Looser, paradigm-neutral normalisation for VALUE comparison: strings are stripped and case-folded,
    numeric-looking strings ('1,234', '5.0') become numbers, numbers use 6 significant digits."""
    if x is None:
        return None
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(f"{float(x):.6g}")
    if isinstance(x, bytes):
        x = x.decode("utf-8", "replace")
    t = str(x).strip()
    try:
        return float(f"{float(t.replace(',', '')):.6g}")
    except ValueError:
        return t.casefold()


def value_hash(values) -> str:
    """Multiset hash of a FLAT list of cell values (column layout and row order ignored)."""
    keyed = sorted(repr(norm_value(v)) for v in values)
    return hashlib.sha1("\n".join(keyed).encode()).hexdigest()


def set_hash(rows) -> str:
    """BIRD/Spider-style comparison: the SET of normalised row tuples (duplicates and order ignored)."""
    keyed = sorted({repr(tuple(norm_cell(c) for c in r)) for r in rows})
    return hashlib.sha1("\n".join(keyed).encode()).hexdigest()


def struct_hash(rows) -> str:
    """Structured comparison: the multiset of rows after putting columns in a canonical order, so column ORDER is
    ignored but every value stays tied to its row and column (unlike value_hash, which flattens cells). Columns are
    ordered by a signature of their own value multiset; columns with identical multisets keep their relative order."""
    rows = [tuple(norm_cell(c) for c in r) for r in rows]
    if not rows:
        return hashlib.sha1(b"").hexdigest()
    sig = [hashlib.sha1("\n".join(sorted(repr(r[j]) for r in rows)).encode()).hexdigest() for j in range(len(rows[0]))]
    order = sorted(range(len(sig)), key=lambda j: sig[j])
    keyed = sorted(repr(tuple(r[j] for j in order)) for r in rows)
    return hashlib.sha1("\n".join(keyed).encode()).hexdigest()


def flatten(rows):
    return [c for r in rows for c in r]


def run_sql(con, sql, timeout=TIMEOUT_S):
    deadline = time.time() + timeout
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 200000)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.set_progress_handler(None, 0)


def pair_key(a: str, b: str):
    return tuple(sorted([a.lower(), b.lower()]))


@dataclass
class Gold:
    qid: str
    source: str
    db_id: str
    split: str
    question: str
    knowledge: str
    gold_sql: str
    operation: str
    n_tables: int
    rows_mode: str
    required_tables: list
    required_columns: dict            # table -> [cols]
    required_joins: set               # {(a, b)} lowercase "table.col" pairs
    row_ids: dict = field(default_factory=dict)   # table -> set(rowid)
    _answer_hash: str | None = None
    _value_hash: str | None = None
    _set_hash: str | None = None
    _struct_hash: str | None = None

    @property
    def db_path(self):
        return paths.sqlite_path(self.source, self.db_id)

    def _load_hashes(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        f = CACHE / "gold_answer_hash.json"
        cache = json.load(open(f)) if f.exists() else {}
        if any(k not in cache for k in (self.qid, f"{self.qid}|v", f"{self.qid}|s", f"{self.qid}|t")):
            con = lenient(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True))
            rows = run_sql(con, self.gold_sql)
            con.close()
            cache[self.qid] = canon_hash(rows)
            cache[f"{self.qid}|v"] = value_hash(flatten(rows))
            cache[f"{self.qid}|s"] = set_hash(rows)
            cache[f"{self.qid}|t"] = struct_hash(rows)
            json.dump(cache, open(f, "w"))
        self._answer_hash, self._value_hash = cache[self.qid], cache[f"{self.qid}|v"]
        self._set_hash, self._struct_hash = cache[f"{self.qid}|s"], cache[f"{self.qid}|t"]

    def answer_hash(self) -> str:
        if self._answer_hash is None:
            self._load_hashes()
        return self._answer_hash

    def set_hash(self) -> str:
        if self._set_hash is None:
            self._load_hashes()
        return self._set_hash

    def struct_hash(self) -> str:
        if self._struct_hash is None:
            self._load_hashes()
        return self._struct_hash

    def value_hash(self) -> str:
        if self._value_hash is None:
            self._load_hashes()
        return self._value_hash


def load_gold(limit: int | None = None) -> dict[str, Gold]:
    ev = [json.loads(l) for l in open(paths.active()["evidence_draft"])]
    rows = {json.loads(l)["qid"]: json.loads(l) for l in open(paths.active()["evidence_rows"])}
    out = {}
    for e in ev[:limit]:
        req = e["evidence"]["required"]
        joins = {pair_key(*k["pair"]) for k in req["join_keys"] if len(k["pair"]) == 2}
        joins |= {pair_key(j["outer"], j["inner"]) for j in req["implicit_joins"]
                  if j["outer"] and j["inner"] and j["outer"] != j["inner"]}
        g = Gold(qid=e["qid"], source=e["source"], db_id=e["db_id"], split=e["split"],
                 question=e["question"], knowledge=e["knowledge"], gold_sql=e["gold_sql"],
                 operation=e["operation"], n_tables=e["n_tables"], rows_mode=req["rows"]["mode"],
                 required_tables=req["tables"], required_columns=req["columns"], required_joins=joins)
        ids: dict[str, set] = {}
        for st in rows[e["qid"]]["steps"]:
            for t, info in st["tables"].items():
                if info["rowids"] is None:             # large set: recompute from the defining SQL
                    ids.setdefault(t, set()).update(_large_ids(g, e, st["id"], t))
                else:
                    ids.setdefault(t, set()).update(info["rowids"])
        g.row_ids = ids
        out[g.qid] = g
    return out


def _large_ids(g: Gold, e: dict, step_id: str, table: str):
    f = CACHE / "large_row_ids.json"
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = json.load(open(f)) if f.exists() else {}
    key = f"{g.qid}|{step_id}|{table}"
    if key not in cache:
        st = next(s for s in e["evidence"]["required"]["steps"] if s["id"] == step_id)
        con = lenient(sqlite3.connect(f"file:{g.db_path}?mode=ro", uri=True))
        got = set()
        for row in run_sql(con, st["evidence_sql"]):
            for tname, rid in zip(st["evidence_tables"], row):
                if tname == table and rid is not None:
                    got.add(rid)
        con.close()
        cache[key] = sorted(got)
        json.dump(cache, open(f, "w"))
    return cache[key]
