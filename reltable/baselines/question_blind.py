"""Question-blind control: ranks tables from SCHEMA FEATURES ONLY (never sees the question).

Trained to predict which tables are REQUIRED, with out-of-fold predictions grouped by database, so a
database never scores itself. If this nears a real system's table recall at high decoy counts, the scale
curve is measuring schema shortcuts (see reports/shortcut_probe.json), not retrieval.

Output has ranked_tables / selected_tables only (no SQL): answer metrics stay unavailable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import GroupKFold

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402
from reltable.evaluation import variants  # noqa: E402
from reltable.evaluation.gold import load_gold  # noqa: E402
from reltable.schema_features import relational_feats, row  # noqa: E402

TOPK = 5


def variant_rows(g, level):
    pool = variants._pool()
    own = pool[f"{g.source}/{g.db_id}"]
    ds = variants.manifest(g.qid)["decoys"][:level]
    present = {(d["source"], d["db_id"], d["table"]) for d in ds}
    fin = {}
    for t, mt in own.items():
        for (_f, ref, _to) in mt["fks"]:
            fin[ref] = fin.get(ref, 0) + 1
    vt = [(("o", t), mt, [("o", r) for (_f, r, _to) in mt["fks"] if r in own]) for t, mt in own.items()]
    for d in ds:
        mt = pool[f"{d['source']}/{d['db_id']}"][d["table"]]
        vt.append((("d", d["source"], d["db_id"], d["table"]), mt,
                   [("d", d["source"], d["db_id"], r) for (_f, r, _to) in mt["fks"]
                    if (d["source"], d["db_id"], r) in present]))
    rel = relational_feats(vt)
    out = []
    for t, mt in own.items():
        out.append((t, {**row(mt, t, len(mt["fks"]), fin.get(t, 0), t),
                        **{"rel_" + a: b for a, b in rel[("o", t)].items()}}))
    dfin = {}
    for d in ds:
        for (_f, ref, _to) in pool[f"{d['source']}/{d['db_id']}"][d["table"]]["fks"]:
            k = (d["source"], d["db_id"], ref)
            if k in present:
                dfin[k] = dfin.get(k, 0) + 1
    for d in ds:
        mt = pool[f"{d['source']}/{d['db_id']}"][d["table"]]
        k = (d["source"], d["db_id"], d["table"])
        fo = sum(1 for (_f, ref, _to) in mt["fks"] if (d["source"], d["db_id"], ref) in present)
        out.append((d["name"], {**row(mt, d["name"], fo, dfin.get(k, 0), d["name"]),
                                **{"rel_" + a: b for a, b in rel[("d", d["source"], d["db_id"], d["table"])].items()}}))
    return out


def run(out_path, levels=(0, 10, 50, 100), limit=None, seed=0):
    gold = load_gold(limit)
    X, y, qids, lvls, names, groups = [], [], [], [], [], []
    for g in gold.values():
        req = {t.lower() for t in g.required_tables}
        for lvl in levels:
            for name, feats in variant_rows(g, lvl):
                X.append(feats); y.append(int(name.lower() in req))
                qids.append(g.qid); lvls.append(lvl); names.append(name); groups.append(f"{g.source}/{g.db_id}")
    cols = list(X[0])
    M = np.array([[float(r[c]) for c in cols] for r in X])
    y, groups = np.array(y), np.array(groups)
    score = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(M, y, groups):
        m = GradientBoostingClassifier(n_estimators=150, max_depth=3, random_state=seed).fit(M[tr], y[tr])
        score[te] = m.predict_proba(M[te])[:, 1]
    by = {}
    for i, (q, l, n) in enumerate(zip(qids, lvls, names)):
        by.setdefault((q, l), []).append((-score[i], n.lower(), n))
    with open(out_path, "w") as f:
        for (q, l), lst in by.items():
            ranked = [n for _s, _k, n in sorted(lst)]
            f.write(json.dumps({"system": "question_blind", "qid": q, "level": l,
                                "ranked_tables": ranked, "selected_tables": ranked[:TOPK],
                                "cost": {"latency_s": 0.0, "llm_calls": 0}}) + "\n")
    return len(by)
