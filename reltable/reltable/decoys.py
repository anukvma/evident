"""Decoy-table scale variants (Component B).

A variant = the question's own database (untouched, attached read-only) + N real tables copied
from OTHER BIRD/Spider databases. Variants are stored as manifests, not data: every decoy is a
(source, db, table) triple plus its name in the variant, and rows are the first `row_cap` rows
by rowid. `materialise` rebuilds a variant deterministically.

Controls (each one is verified in build/05):
  * nested levels      : the 10-decoy set is a prefix of the 50-decoy set, itself a prefix of 100
  * no look-alikes     : decoys whose table name equals any own-DB table, or whose column-name
                         Jaccard with any own-DB table is >= threshold, are excluded (they could be a
                         second valid source for the question and make "irrelevant" false)
  * no leakage         : decoys never come from the question's own DB (or any DB with the same id),
                         from the other split's pilot DBs, or from Spider DEV databases
  * no giveaways       : decoy DDL is regenerated from PRAGMAs, FKs kept only when their target is in
                         the variant, so "has a valid FK" does not reveal which tables are real
  * name collisions    : only possible between decoys; resolved with a numeric suffix
"""
from __future__ import annotations

import collections
import json
import os
import random
import re
import sqlite3
from pathlib import Path

from . import paths


def _ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def table_meta(con, t):
    info = con.execute(f'pragma table_info("{t}")').fetchall()
    fks = [(r[3], r[2], r[4]) for r in con.execute(f'pragma foreign_key_list("{t}")')]
    return {
        "cols": [r[1] for r in info], "types": [r[2] or "" for r in info],
        "notnull": [bool(r[3]) for r in info], "pk": [r[5] for r in info],
        "fks": fks,
        "n_rows": con.execute(f'select count(*) from "{t}"').fetchone()[0],
        "has_rowid": _has_rowid(con, t),
    }


def _has_rowid(con, t):
    """WITHOUT ROWID tables (3 in BIRD train) cannot be copied by rowid or carry row evidence."""
    try:
        con.execute(f'select rowid from "{t}" limit 1').fetchall()
        return True
    except sqlite3.Error:
        return False


def build_pool(cache: Path):
    """Every table of every BIRD-dev and Spider database (and, for the v1 pool under data/v1, every BIRD-train
    database), with column/FK/row-count metadata."""
    if cache.exists():
        return json.load(open(cache))
    pool = {}
    dbs = [("bird", d) for d in sorted(os.listdir(paths.BIRD / "dev_databases"))
           if paths.sqlite_path("bird", d).exists()]
    if Path(cache).parent == paths.DATA_V1:
        dbs += [("bird", d) for d in sorted(os.listdir(paths.BIRD_TRAIN / "train_databases"))
                if paths.sqlite_path("bird", d).exists()]
    dbs += [("spider", d) for d in sorted(os.listdir(paths.SPIDER / "database"))
            if paths.sqlite_path("spider", d).exists()]
    for s, d in dbs:
        con = _ro(paths.sqlite_path(s, d))
        pool[f"{s}/{d}"] = {t: table_meta(con, t) for (t,) in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'")}
        con.close()
    json.dump(pool, open(cache, "w"))
    return pool


def jaccard(a, b):
    a, b = {x.lower() for x in a}, {x.lower() for x in b}
    return len(a & b) / len(a | b) if a | b else 0.0


def fk_bfs_order(tables: dict, rng):
    """Tables of one DB in FK-connected order from a random start (keeps decoy groups coherent)."""
    names = sorted(tables)
    rng.shuffle(names)
    adj = {t: set() for t in names}
    for t, m in tables.items():
        for (_f, ref, _to) in m["fks"]:
            if ref in adj:
                adj[t].add(ref); adj[ref].add(t)
    seen, order = set(), []
    for start in names:
        if start in seen:
            continue
        queue = [start]
        seen.add(start)
        while queue:
            cur = queue.pop(0)
            order.append(cur)
            for nb in sorted(adj[cur]):
                if nb not in seen:
                    seen.add(nb); queue.append(nb)
    return order


def select_decoys(pool, rec, split_of, cfg, n_max=None, skip=(), used_names=(), tag=""):
    """Ordered decoy list for one pilot record (length up to n_max, default max(levels)). Deterministic.

    Extension mode: pass the already-chosen decoys' (source, db, table) triples as `skip`, their variant names as
    `used_names`, and a `tag`; the result is appended AFTER the existing list, so every existing prefix (and thus every
    existing level) is untouched."""
    rng = random.Random(f"{cfg['seed']}:{rec['qid']}{tag}")
    skip = set(skip)
    own_key = f"{rec['source']}/{rec['db_id']}"
    own = pool[own_key]
    own_names = {t.lower() for t in own}
    own_cols = {t: m["cols"] for t, m in own.items()}
    required = rec["evidence"]["required"]["tables"]
    spider_dev = {k for k in split_of if k.startswith("spider/")}
    split = rec["split"]

    def eligible_db(k):
        s, d = k.split("/", 1)
        if k == own_key or d == rec["db_id"]:
            return False
        if k in split_of:                                   # a pilot DB: same split only
            return split_of[k] == split
        if s == "spider" and k not in spider_dev and k in spider_train:
            return True
        return False
    spider_train = {k for k in pool if k.startswith("spider/")} - set(split_of) - {
        f"spider/{d}" for d in all_spider_dev_dbs()}
    cands = [k for k in sorted(pool) if eligible_db(k)]
    same_src = [k for k in cands if k.split("/")[0] == rec["source"] and k in split_of]
    rest = [k for k in cands if k not in same_src]
    rng.shuffle(same_src); rng.shuffle(rest)
    stream, i, j = [], 0, 0                                 # interleave: avoids a source-style giveaway
    while i < len(same_src) or j < len(rest):
        if i < len(same_src):
            stream.append(same_src[i]); i += 1
        if j < len(rest):
            stream.append(rest[j]); j += 1

    n_max = n_max or max(cfg["levels"])
    tables = ((k, t) for k in stream for t in fk_bfs_order(pool[k], rng))   # lazy: rng is consumed in the original order
    return _choose(pool, own, own_names, own_cols, required, tables, cfg, n_max, skip, used_names)


def _choose(pool, own, own_names, own_cols, required, tables, cfg, n_max, skip=(), used_names=()):
    """Filter candidate (db_key, table) pairs in the given order, size-match them to the question's own DB, and order
    them so every nested prefix tracks the target size distribution. Shared by the pilot and v1 selectors."""
    skip = set(skip)
    excluded = {"empty": 0, "same_name_as_own": 0, "look_alike": 0}
    eligible = []                                           # (db_key, table, meta, bin) in stream order
    for k, t in tables:
        m = pool[k][t]
        if (k.split("/")[0], k.split("/", 1)[1], t) in skip:
            continue
        if m["n_rows"] == 0:
            excluded["empty"] += 1; continue
        if m.get("has_rowid") is False:
            excluded["no_rowid"] = excluded.get("no_rowid", 0) + 1; continue
        if t.lower() in own_names:
            excluded["same_name_as_own"] += 1; continue
        jmax = max((jaccard(m["cols"], c) for c in own_cols.values()), default=0.0)
        if jmax >= cfg["jaccard_threshold"]:
            excluded["look_alike"] += 1; continue
        if cfg.get("answer_guard") and cfg["answer_guard"](k, t, m):
            excluded["contains_question_literal"] = excluded.get("contains_question_literal", 0) + 1; continue
        eligible.append((k, t, m, size_bin(m["n_rows"]), jmax))

    # --- size matching: decoys follow the row-count distribution of the question's own DB,
    # otherwise table size alone reveals which tables are real (measured: BIRD median 4,500 vs 15).
    own_bins = [size_bin(m["n_rows"]) for m in own.values()]
    quota = {b: round(n_max * own_bins.count(b) / len(own_bins)) for b in set(own_bins)}
    while sum(quota.values()) != n_max:                     # fix rounding on the largest bin
        big = max(quota, key=quota.get)
        quota[big] += n_max - sum(quota.values())
    taken, by_bin = set(), collections.defaultdict(list)
    for i, e in enumerate(eligible):
        if len(by_bin[e[3]]) < quota.get(e[3], 0):
            by_bin[e[3]].append(i); taken.add(i)
    matched = sum(len(v) for v in by_bin.values())
    for i, e in enumerate(eligible):                        # relax: fill any shortfall in stream order
        if matched >= n_max:
            break
        if i not in taken:
            by_bin[e[3]].append(i); taken.add(i); matched += 1
    # --- order so EVERY prefix (10, 50, 100) tracks the target distribution (largest deficit first)
    queues = {b: list(v) for b, v in by_bin.items()}
    got = collections.Counter()
    order = []
    for pos in range(n_max):
        def deficit(b):
            want = quota.get(b, 0) * (pos + 1) / n_max
            return want - got[b] if queues.get(b) else -1e9
        b = max(queues, key=deficit)
        order.append(queues[b].pop(0)); got[b] += 1
    used, out = set(own_names) | {u.lower() for u in used_names}, []
    for i in order:
        k, t, m, bn, jmax = eligible[i]
        name, n = t, 1
        while name.lower() in used:
            n += 1; name = f"{t}_{n}"
        used.add(name.lower())
        jreq = max((jaccard(m["cols"], own_cols[r]) for r in required if r in own_cols), default=0.0)
        out.append({"source": k.split("/")[0], "db_id": k.split("/", 1)[1], "table": t,
                    "name": name, "n_rows": m["n_rows"], "size_bin": bn,
                    "rows_kept": min(m["n_rows"], cfg["row_cap"]),
                    "max_jaccard_own": round(jmax, 3), "max_jaccard_required": round(jreq, 3)})
    excluded["quota_matched"] = sum(min(len(v), quota.get(b, 0)) for b, v in by_bin.items())
    return out, excluded


def size_bin(n):
    """log10 bin of a row count: 0:<10, 1:<100, 2:<1000, 3:<10000, 4:>=10000."""
    return min(4, max(0, len(str(max(int(n), 1))) - 1))


_SPIDER_DEV = None


def all_spider_dev_dbs():
    global _SPIDER_DEV
    if _SPIDER_DEV is None:
        _SPIDER_DEV = {q["db_id"] for q in json.load(open(paths.SPIDER / "dev.json"))}
    return _SPIDER_DEV


def decoy_ddl(pool, decoy, name_map):
    """CREATE TABLE from PRAGMA metadata. FKs only to tables present in the same variant,
    pointing at their variant names."""
    m = pool[f"{decoy['source']}/{decoy['db_id']}"][decoy["table"]]
    parts = []
    pks = sorted((p, c) for p, c in zip(m["pk"], m["cols"]) if p)
    single_pk = len(pks) == 1
    for c, ty, nn, p in zip(m["cols"], m["types"], m["notnull"], m["pk"]):
        s = f'"{c}" {ty}'.rstrip()
        if single_pk and p:
            s += " PRIMARY KEY"
        elif nn:
            s += " NOT NULL"
        parts.append(s)
    if len(pks) > 1:
        parts.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for _p, c in pks) + ")")
    for (frm, ref, to) in m["fks"]:
        key = (decoy["source"], decoy["db_id"], ref)
        if key in name_map and to:
            parts.append(f'FOREIGN KEY ("{frm}") REFERENCES "{name_map[key]}" ("{to}")')
    return f'CREATE TABLE "{decoy["name"]}" (\n  ' + ",\n  ".join(parts) + "\n)"


def materialise(pool, rec_source, rec_db, decoys, row_cap):
    """In-memory DB holding the decoys; the original DB is attached read-only as `orig`.
    Unqualified names resolve main-first then attached, so gold SQL runs unchanged."""
    mem = sqlite3.connect(":memory:", uri=True)
    mem.text_factory = lambda b: b.decode("utf-8", "replace")
    mem.execute(f"ATTACH DATABASE 'file:{paths.sqlite_path(rec_source, rec_db)}?mode=ro' AS orig")
    name_map = {(d["source"], d["db_id"], d["table"]): d["name"] for d in decoys}
    srcs = {}
    for d in decoys:
        mem.execute(decoy_ddl(pool, d, name_map))
        k = (d["source"], d["db_id"])
        if k not in srcs:
            srcs[k] = _ro(paths.sqlite_path(*k))
        cur = srcs[k].execute(f'select * from "{d["table"]}" order by rowid limit {row_cap}')
        ncol = len(pool[f"{k[0]}/{k[1]}"][d["table"]]["cols"])
        mem.executemany(f'insert into "{d["name"]}" values ({",".join("?" * ncol)})', cur)
    for c in srcs.values():
        c.close()
    mem.commit()
    return mem


# ================================================================ DREDGE v1 (build/13)
def eligible_dbs_v1(pool, rec, db_split):
    """Databases a v1 question may draw decoys from: other databases of its own split and, for test questions only,
    the Spider-train databases (the test-only pool; they are never a question source). Never its own database or one
    with the same id, and never a database of another split (a trained system must not meet test decoys in training)."""
    own_key, split = f"{rec['source']}/{rec['db_id']}", rec["split"]
    out = []
    for k in sorted(pool):
        if k == own_key or k.split("/", 1)[1] == rec["db_id"]:
            continue
        s = db_split.get(k)
        if s == split or (s is None and split == "test" and paths.origin(*k.split("/", 1)) == "spider_train"):
            out.append(k)
    return out


def select_decoys_v1(pool, rec, db_split, cfg, tier="random", scores=None, guard=None):
    """Ordered decoy list (up to max(cfg['levels'])) for one v1 record. Deterministic.

    random: databases in shuffled order, same-source question databases interleaved with the rest (as in the pilot),
            tables of a database in FK-connected order.
    hard:   tables in descending `scores[(db_key, table)]` (similarity to the question and its required tables).
    Both tiers apply the pilot's look-alike, empty-table and size-matching rules, plus `guard(db_key, table, meta)`,
    which rejects tables that could answer the question (see build/13)."""
    rng = random.Random(f"{cfg['seed']}:{rec['qid']}:v1:{tier}")
    own = pool[f"{rec['source']}/{rec['db_id']}"]
    own_names = {t.lower() for t in own}
    own_cols = {t: m["cols"] for t, m in own.items()}
    cands = eligible_dbs_v1(pool, rec, db_split)
    if tier == "random":
        same_src = [k for k in cands if k.split("/")[0] == rec["source"] and k in db_split]
        rest = [k for k in cands if k not in same_src]
        rng.shuffle(same_src); rng.shuffle(rest)
        stream = [x for pair in zip(same_src, rest) for x in pair]
        longer = same_src if len(same_src) > len(rest) else rest
        stream += longer[min(len(same_src), len(rest)):]
        tables = ((k, t) for k in stream for t in fk_bfs_order(pool[k], rng))
    elif tier == "hard":
        allt = [(k, t) for k in cands for t in pool[k]]
        tables = sorted(allt, key=lambda kt: (-scores.get(kt, 0.0), kt))
    else:
        raise ValueError(tier)
    c = dict(cfg, answer_guard=guard)
    return _choose(pool, own, own_names, own_cols, rec["evidence"]["required"]["tables"], tables, c, max(cfg["levels"]))


_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)'
_LIT = re.compile(rf"((?:{_IDENT}\.)?{_IDENT})\s*(=|!=|<>|\bNOT\s+LIKE\b|\bLIKE\b|\bNOT\s+IN\s*\(|\bIN\s*\()\s*((?:'[^']*'\s*,?\s*)+)", re.I)


def question_literals(sql):
    """[(column_name, literal)] for string literals compared with a column (=, !=, LIKE, IN). The column is the last
    identifier before the operator, without table alias and quotes. Used by the answer guard."""
    out = []
    for m in _LIT.finditer(sql):
        col = re.sub(r"""[`"\[\]]""", "", m.group(1).split(".")[-1]).strip()
        for lit in re.findall(r"'([^']*)'", m.group(3)):
            if len(lit.strip("% ")) >= 2:
                out.append((col, lit))
    return out
