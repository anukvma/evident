"""Human-adjudicated ALTERNATIVE reference queries, for equivalence-aware evidence sufficiency.

Strict sufficiency asks: does the GOLD query still return the gold answer on the rows the system retrieved? A system that takes
an equivalent route (skips a bridge table) fails that test although it is right. An alternative reference is a second query
that a human has judged semantically equivalent to the gold one; evidence is `evidence_sufficient_eq` if the gold query OR any
CONFIRMED alternative reproduces the gold answer on the retrieved rows.

File: data/alt_reference_sql.json
  {qid: {"alternatives": [{"sql": "...", "status": "proposed|confirmed|rejected", "note": "...", "seen_in": [...],
                           "reviewed_by": "..."}]}}
Only status == "confirmed" is used by the scorer. Proposals come from build/07_propose_alt_refs.py (predictions that were
correct but strictly insufficient) and MUST be reviewed by a person: coincidences (right answer, different meaning) must be rejected.
"""
from __future__ import annotations

import json

from .. import paths

PATH = paths.DATA / "alt_reference_sql.json"
_cache = None


def load(force=False):
    global _cache
    if _cache is None or force:
        _cache = json.load(open(PATH)) if PATH.exists() else {}
    return _cache


def confirmed(qid):
    return [a["sql"] for a in load().get(qid, {}).get("alternatives", []) if a.get("status") == "confirmed"]
