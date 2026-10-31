"""Open a question's database at a given decoy level (0 = original)."""
from __future__ import annotations

import json
import sqlite3

from .. import decoys as D
from .. import paths
from ..minidb import lenient

_CFG = json.load(open(paths.ROOT / "configs" / "decoys.json"))
_POOL = None
_MANIFEST = None


def _pool():
    global _POOL
    if _POOL is None:
        _POOL = D.build_pool(paths.active()["pool"])
    return _POOL


def manifest(qid):
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = {json.loads(l)["qid"]: json.loads(l)
                     for l in open(paths.active()["manifests"])}
    return _MANIFEST[qid]


def level_available(qid, level):
    """v1 manifests list the levels a question's pool can fill; a level it cannot fill is not part of the benchmark for
    that question (neither run nor scored). Pilot manifests have every level."""
    if level == 0:
        return True
    avail = manifest(qid).get("levels_available")
    return avail is None or level in avail


def candidate_tables(g, level):
    """Names of all tables a system sees at this level (own + decoys)."""
    own = list(_pool()[f"{g.source}/{g.db_id}"])
    return own + [d["name"] for d in manifest(g.qid)["decoys"][:level]]


def open_variant(g, level):
    """Read-only connection. Level 0: the original DB. Level N: first N decoys in memory + original attached."""
    if level == 0:
        con = lenient(sqlite3.connect(f"file:{g.db_path}?mode=ro", uri=True))
    else:
        con = D.materialise(_pool(), g.source, g.db_id, manifest(g.qid)["decoys"][:level], _CFG["row_cap"])
        lenient(con)
    con.execute("PRAGMA query_only = ON")
    return con
