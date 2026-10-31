"""Derive structural features and a table/column read-set from a gold SQL query.

The read-set comes from SQLite's authorizer callback, which fires for every real
table/column a statement reads (aliases and subqueries are resolved by SQLite). This is a
*candidate* evidence set only: equivalent queries can read different columns, so it is
reviewed by humans before it becomes gold evidence (see DESIGN.md).
"""
import re
import sqlite3
from pathlib import Path

_AGG = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|TOTAL)\s*\(", re.I)
_JOIN = re.compile(r"\bJOIN\b", re.I)
_SELECT = re.compile(r"\bSELECT\b", re.I)
_SETOP = re.compile(r"\b(UNION|INTERSECT|EXCEPT)\b", re.I)
_GROUP = re.compile(r"\bGROUP\s+BY\b", re.I)
_ORDER = re.compile(r"\bORDER\s+BY\b", re.I)
_WHERE = re.compile(r"\bWHERE\b", re.I)


def read_set(db_path: Path, sql: str):
    """Return ({table: sorted columns read}, error or None). Never executes the query."""
    reads: dict[str, set] = {}

    def auth(action, a1, a2, dbname, source):
        if action == sqlite3.SQLITE_READ and a1 and not a1.startswith("sqlite_"):
            reads.setdefault(a1, set())
            if a2:
                reads[a1].add(a2)
        return sqlite3.SQLITE_OK

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.set_authorizer(auth)
        con.execute("EXPLAIN " + sql)
        err = None
        # SQLite is case-insensitive for identifiers, so the same table can be reported under
        # several spellings; snap everything to the schema's own spelling.
        con.set_authorizer(None)
        canon = {t.lower(): (t, {r[1].lower(): r[1] for r in con.execute(f'pragma table_info("{t}")')})
                 for (t,) in con.execute("select name from sqlite_master where type='table'")}
        merged: dict[str, set] = {}
        for t, cols in reads.items():
            ct, cmap = canon.get(t.lower(), (t, {}))
            merged.setdefault(ct, set()).update(cmap.get(c.lower(), c) for c in cols)
        reads = merged
    except sqlite3.Error as e:  # invalid gold SQL is recorded, not hidden
        err = str(e)
    finally:
        con.close()
    return {t: sorted(c) for t, c in reads.items()}, err


def features(sql: str, analysis: dict) -> dict:
    """Structural features from the sqlglot analysis (see sqlast.py)."""
    fl = analysis["flags"]
    f = {
        "n_tables": len(analysis["reads"]),
        "n_joins": len(_JOIN.findall(sql)),            # JOIN keywords (kept for reference)
        "n_join_keys": len(analysis["join_keys"]),
        "n_implicit_joins": len(analysis["implicit_joins"]),
        "has_agg": fl["aggregate"], "has_group": fl["group"], "has_order": fl["order"],
        "ranking": fl["ranking"], "negation": fl["negation"], "set_op": fl["set_op"],
        "has_where": bool(_WHERE.search(sql)),
        "nested": fl["nested"] or fl["set_op"],
    }
    f["operation"] = operation_label(f)
    return f


def operation_label(f: dict) -> str:
    """Single primary label, most complex wins. Mirrors the operations in Table 2."""
    if f["nested"]:
        return "nested"
    if f["has_group"]:
        return "group"
    if f["has_agg"]:
        return "aggregate"
    if f["n_tables"] > 1:
        return "join"
    if f["has_where"] or f["has_order"]:
        return "filter"
    return "lookup"
