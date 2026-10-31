"""Schema-level table features (no question text, no cell values).

Used by the shortcut probe (build/06) and the question-blind baseline, so both see identical features.
"""
import collections
import math
import re

import numpy as np


def style_feats(name, meta):
    cols, types = meta["cols"], meta["types"]
    n = max(len(cols), 1)
    camel = bool(re.search(r"[a-z][A-Z]", name))
    ty = [t.lower() for t in types]
    return {
        "name_len": len(name), "name_has_underscore": "_" in name, "name_camel": camel,
        "name_all_lower": name == name.lower(), "name_has_upper": name != name.lower(),
        "col_avg_len": sum(map(len, cols)) / n,
        "col_frac_underscore": sum("_" in c for c in cols) / n,
        "col_frac_space": sum((" " in c or "(" in c) for c in cols) / n,
        "col_frac_camel": sum(bool(re.search(r"[a-z][A-Z]", c)) for c in cols) / n,
        "col_frac_lower": sum(c == c.lower() for c in cols) / n,
        "col_frac_id": sum(c.lower().endswith("id") for c in cols) / n,
        "type_upper": sum(t != t.lower() and t != "" for t in types) / n,
        "type_has_len": sum("(" in t for t in types) / n,
        "type_text": sum(t in ("text", "varchar") or t.startswith("varchar") for t in ty) / n,
        "type_number": sum(t == "number" for t in ty) / n,
        "type_int": sum("int" in t for t in ty) / n,
        "type_real": sum(t in ("real", "float", "double") for t in ty) / n,
        "type_empty": sum(t == "" for t in ty) / n,
    }


def row(meta, name, fk_out, fk_in, table_name):
    f = {"log_rows": math.log10(meta["n_rows"] + 1)}
    f.update({"n_cols": len(meta["cols"]), "has_pk": any(meta["pk"]),
              "n_pk": sum(1 for p in meta["pk"] if p), "fk_out": fk_out, "fk_in": fk_in})
    f.update(style_feats(table_name, meta))
    return f



def relational_feats(tables):
    """tables: list of (key, meta, fk_pairs) in one variant -> per-table within-variant features.
    Own tables come from ONE database (shared naming/key columns, one FK component); decoys from many.
    A system that clusters tables by schema similarity could exploit this; measure how much."""
    cols = {k: {c.lower() for c in m["cols"]} for k, m, _ in tables}
    keys = list(cols)
    # FK components (union-find over valid FKs inside the variant)
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for k, _m, fks in tables:
        for r in fks:
            if r in parent:
                parent[find(k)] = find(r)
    comp = collections.Counter(find(k) for k in keys)
    stylesig = {k: (any(c != c.lower() for c in m["cols"]), any("_" in c for c in m["cols"]),
                    any(" " in c for c in m["cols"]), any(t != t.lower() and t for t in m["types"]))
                for k, m, _ in tables}
    sigc = collections.Counter(stylesig.values())
    out = {}
    for k in keys:
        others = [o for o in keys if o != k]
        shared = [len(cols[k] & cols[o]) for o in others]
        jac = [len(cols[k] & cols[o]) / len(cols[k] | cols[o]) if cols[k] | cols[o] else 0 for o in others]
        out[k] = {"comp_size": comp[find(k)], "n_share_col": sum(1 for x in shared if x > 0),
                  "max_jaccard_other": max(jac) if jac else 0, "mean_jaccard_other": float(np.mean(jac)) if jac else 0,
                  "style_group_size": sigc[stylesig[k]]}
    return out
