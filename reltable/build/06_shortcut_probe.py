"""Step 6: shortcut probe for the decoy design.

Question: can a classifier tell the question's REAL tables from DECOYS using only schema-level
features (no question text, no cell values)? If it can, accuracy-vs-scale curves would partly
measure that shortcut instead of search-space growth.

Setup: level-100 variants (own tables = 0, decoys = 1); features from the pool metadata; grouped
5-fold CV by the question's database (a database never appears in both train and test).
Feature sets are cumulative: size -> +structure -> +naming/type style.
Second probe (no decoys involved): among a question's own tables, can the same features predict
which are REQUIRED? (relevant tables being systematically bigger would help any retriever).

Chance AUC = 0.5. Usage: python build/06_shortcut_probe.py
Output: reports/shortcut_probe.json
"""
import collections
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import decoys as D, paths  # noqa: E402
from reltable.schema_features import relational_feats, row  # noqa: E402

LEVEL = 100


SETS = {"size": ["log_rows"],
        "size+structure": ["log_rows", "n_cols", "has_pk", "n_pk", "fk_out", "fk_in"]}


def auc_cv(X, y, g, seed=0):
    if len(set(y)) < 2:
        return None
    pred = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(X, y, g):
        m = GradientBoostingClassifier(n_estimators=150, max_depth=3, random_state=seed)
        m.fit(X[tr], y[tr])
        pred[te] = m.predict_proba(X[te])[:, 1]
    return float(roc_auc_score(y, pred))


def main():
    pool = D.build_pool(paths.DATA / "decoy_pool.json")
    man = [json.loads(l) for l in open(paths.DATA / "decoy_manifests.jsonl")]
    ev = {json.loads(l)["qid"]: json.loads(l) for l in open(paths.DATA / "evidence_draft.jsonl")}
    rows, labels, groups, srcs, required = [], [], [], [], []
    for m in man:
        own = pool[f"{m['source']}/{m['db_id']}"]
        req = set(ev[m["qid"]]["evidence"]["required"]["tables"])
        ds = m["decoys"][:LEVEL]
        present = {(d["source"], d["db_id"], d["table"]) for d in ds}
        fin = collections.Counter()
        for t, mt in own.items():
            for (_f, ref, _to) in mt["fks"]:
                fin[ref] += 1
        dfin = collections.Counter()
        for d in ds:
            for (_f, ref, _to) in pool[f"{d['source']}/{d['db_id']}"][d["table"]]["fks"]:
                if (d["source"], d["db_id"], ref) in present:
                    dfin[(d["source"], d["db_id"], ref)] += 1
        vt = [(("o", t), mt, [("o", r) for (_f, r, _to) in mt["fks"] if r in own]) for t, mt in own.items()]
        for d in ds:
            mt = pool[f"{d['source']}/{d['db_id']}"][d["table"]]
            vt.append((("d", d["source"], d["db_id"], d["table"]), mt,
                       [("d", d["source"], d["db_id"], r) for (_f, r, _to) in mt["fks"]
                        if (d["source"], d["db_id"], r) in present]))
        # fix decoy FK targets to full keys
        vt = [(k, mt, [r if r[0] == "o" else (r[0], r[1], r[2], r[3]) for r in fks]) for k, mt, fks in vt]
        rel = relational_feats(vt)
        for t, mt in own.items():
            rows.append({**row(mt, t, len(mt["fks"]), fin[t], t), **{"rel_" + a: b for a, b in rel[("o", t)].items()}})
            labels.append(0); groups.append(f"{m['source']}/{m['db_id']}"); srcs.append(m["source"])
            required.append(int(t in req))
        for d in ds:
            mt = pool[f"{d['source']}/{d['db_id']}"][d["table"]]
            k = (d["source"], d["db_id"], d["table"])
            fo = sum(1 for (_f, ref, _to) in mt["fks"] if (d["source"], d["db_id"], ref) in present)
            rows.append({**row(mt, d["name"], fo, dfin[k], d["name"]),
                         **{"rel_" + a: b for a, b in rel[("d", d["source"], d["db_id"], d["table"])].items()}})
            labels.append(1); groups.append(f"{m['source']}/{m['db_id']}"); srcs.append(m["source"])
            required.append(-1)
    feats = list(rows[0])
    relf = [f for f in feats if f.startswith("rel_")]
    base = [f for f in feats if not f.startswith("rel_")]
    SETS["size+structure+style"] = base
    SETS["+within-variant relational"] = feats
    X_all = np.array([[float(r[f]) for f in feats] for r in rows])
    y = np.array(labels); g = np.array(groups); s = np.array(srcs); rq = np.array(required)
    idx = {f: i for i, f in enumerate(feats)}
    out = {"level": LEVEL, "rows": int(len(y)), "decoy_share": float(y.mean()), "decoy_vs_real": {},
           "single_feature_auc_all": {}, "required_vs_other_own": {}}
    for scope in ("all", "bird", "spider"):
        msk = np.ones(len(y), bool) if scope == "all" else (s == scope)
        out["decoy_vs_real"][scope] = {}
        for name, cols in SETS.items():
            X = X_all[msk][:, [idx[c] for c in cols]]
            out["decoy_vs_real"][scope][name] = auc_cv(X, y[msk], g[msk])
    # single features (no model): AUC of the raw feature, oriented to >= 0.5
    for f in feats:
        a = roc_auc_score(y, X_all[:, idx[f]])
        out.setdefault("single_feature_is_relational", {})[f] = f in relf
        out["single_feature_auc_all"][f] = round(max(a, 1 - a), 3)
    out["single_feature_auc_all"] = dict(sorted(out["single_feature_auc_all"].items(),
                                                key=lambda kv: -kv[1]))
    # required vs non-required among own tables only
    own_mask = y == 0
    for scope in ("all", "bird", "spider"):
        msk = own_mask & (np.ones(len(y), bool) if scope == "all" else (s == scope))
        res = {}
        for name, cols in SETS.items():
            res[name] = auc_cv(X_all[msk][:, [idx[c] for c in cols]], rq[msk], g[msk])
        res["log_rows_only_raw_auc"] = float(roc_auc_score(rq[msk], X_all[msk][:, idx["log_rows"]]))
        out["required_vs_other_own"][scope] = res
    # within-question view: a retriever ranks ONE candidate set, so score each question separately
    # (feature direction fixed from the pooled data, not tuned per question)
    qid_of = np.repeat(np.arange(len(man)), [len(pool[f"{m['source']}/{m['db_id']}"]) + len(m["decoys"][:LEVEL])
                                             for m in man])
    out["per_question_auc"] = {}
    for f_ in list(out["single_feature_auc_all"])[:6]:
        sign = 1 if roc_auc_score(y, X_all[:, idx[f_]]) >= 0.5 else -1
        per = {"bird": [], "spider": []}
        for q in range(len(man)):
            mk = qid_of == q
            if len(set(y[mk])) == 2:
                per[man[q]["source"]].append(roc_auc_score(y[mk], sign * X_all[mk][:, idx[f_]]))
        out["per_question_auc"][f_] = {k: {"mean": round(float(np.mean(v)), 3),
                                           "share_questions_auc_ge_0.7": round(float(np.mean(np.array(v) >= 0.7)), 3)}
                                       for k, v in per.items()}
    json.dump(out, open(paths.REPORTS / "shortcut_probe.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
