"""Step 3: draft v0 evidence records for the pilot (candidate evidence, NOT gold).

Policy (see DESIGN.md "Evidence policy"):
  * required.tables/columns : tables/columns the gold SQL reads, snapped to schema spelling
  * required.join_keys      : only column pairs the gold SQL actually equates
  * required.implicit_joins : outer-col vs subquery-col links (=, IN, NOT IN, scalar)
  * required.set_relations  : UNION/INTERSECT/EXCEPT between branches (no join key needed)
  * required.steps          : one entry per (sub)query; outer steps depend on inner steps
  * available_fks           : declared FKs among tables read that the query did NOT use;
                              informational, never counted in join-key recall
  * rows.mode               : complete_set when a missing row can change the answer
                              (aggregate, group, ranking, negation, set op, IN/scalar over
                              a ranking/aggregate inner); otherwise predicate
Review reasons (needs_review) vs info notes (notes) are kept apart so the review queue is small.

Usage: python build/03_draft_evidence.py [--input data/v1/test_candidates.jsonl --out data/v1/test_evidence_draft.jsonl --summary reports/v1/test_evidence_draft_summary.json]
Defaults are the pilot files.
Output: data/evidence_draft.jsonl, reports/evidence_draft_summary.json
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import paths, sqlast  # noqa: E402


def components(nodes, edges):
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    return len({find(n) for n in nodes})


def tbl(col):
    return col.split(".")[0] if col else None


def row_mode(fl, implicit):
    if fl["aggregate"] or fl["group"] or fl["ranking"] or fl["negation"] or fl["set_op"]:
        return "complete_set"
    if any(j["kind"] in ("in", "not_in") or j["inner_ranking"] or j["inner_aggregate"]
           for j in implicit):
        return "complete_set"
    return "predicate"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(paths.DATA / "pilot.jsonl"))
    ap.add_argument("--out", default=str(paths.DATA / "evidence_draft.jsonl"))
    ap.add_argument("--summary", default=str(paths.REPORTS / "evidence_draft_summary.json"))
    a = ap.parse_args()
    meta, recs = {}, []
    flags, notes = collections.Counter(), collections.Counter()
    for line in open(a.input):
        r = json.loads(line)
        key = (r["source"], r["db_id"])
        if key not in meta:
            schema, pk, fks = sqlast.load_schema(paths.sqlite_path(*key))
            meta[key] = (pk, fks)
        pk, fks = meta[key]
        an = r["analysis"]
        tables = sorted(an["reads"])
        fk_pairs = {tuple(sorted([f"{c}.{cc}", f"{p}.{pc}"])) for c, cc, p, pc in fks if pc}

        join_keys, used = [], set()
        for j in an["join_keys"]:
            pair = tuple(j["pair"])
            if pair in used:
                continue
            used.add(pair)
            declared = pair in fk_pairs
            # suspect = an undeclared join between two tables' *sole* primary keys (e.g. id = id)
            both_pk = len(pair) == 2 and all(
                pk.get(c.split(".")[0]) == {c.split(".")[1]} for c in pair)
            join_keys.append({"pair": list(pair), "step": j["step"], "declared_fk": declared,
                              "derived": j.get("derived", False),
                              "suspect": (not declared) and both_pk})
        available = [list(p) for p in sorted(fk_pairs)
                     if tbl(p[0]) in tables and tbl(p[1]) in tables and p not in used]

        implicit = an["implicit_joins"]
        edges = [(tbl(k["pair"][0]), tbl(k["pair"][-1])) for k in join_keys]
        edges += [(tbl(j["outer"]), tbl(j["inner"])) for j in implicit if j["outer"] and j["inner"]]
        fl = an["flags"]

        review = []
        if len(tables) > 1 and not fl["set_op"] and components(tables, edges) > 1:
            review.append("tables_not_connected")
        if any(k["suspect"] for k in join_keys):
            review.append("gold_join_suspect")
        if any(j["outer"] is None or j["inner"] is None for j in implicit):
            review.append("implicit_join_unresolved")
        if not r.get("reads_agree", True):
            review.append("ast_sqlite_reads_disagree")
        info = []
        if any(not k["declared_fk"] and not k["suspect"] for k in join_keys):
            info.append("undeclared_join_key_used")
        if implicit:
            info.append("subquery_link")
        if fl["set_op"]:
            info.append("set_operation")
        if fl["negation"]:
            info.append("negation")

        mode = row_mode(fl, implicit)
        steps = []
        for st in an["steps"]:
            inner_cause = [j for j in implicit if j["inner_step"] == st["id"]]
            smode = "complete_set" if (st["aggregate"] or st["group"] or st["ranking"] or any(
                j["kind"] in ("in", "not_in") or j["inner_ranking"] or j["inner_aggregate"]
                for j in inner_cause)) else ("complete_set" if st["kind"] == "root" and mode == "complete_set" else "predicate")
            steps.append({**st, "rows_mode": smode})

        recs.append({
            "qid": r["qid"], "source": r["source"], "db_id": r["db_id"], "split": r["split"],
            "question": r["question"], "knowledge": r["knowledge"], "gold_sql": r["gold_sql"],
            "operation": r["operation"], "n_tables": len(tables), "n_joins": r["n_joins"],
            "evidence": {
                "required": {
                    "tables": tables, "columns": an["reads"], "join_keys": join_keys,
                    "implicit_joins": implicit,
                    "set_relations": an["set_ops"],
                    "steps": steps,
                    "rows": {"mode": mode,
                             "spec": "qualifying rows of gold_sql (to be materialised)"},
                },
                "available_fks": available,
                "supporting": {}, "distracting": {},
            },
            "status": "draft", "needs_review": review, "notes": info,
        })
        flags.update(review or ["clean"])
        notes.update(info)
    with open(a.out, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    summ = {
        "n": len(recs), "review_reasons": dict(flags), "notes": dict(notes),
        "row_mode": dict(collections.Counter(x["evidence"]["required"]["rows"]["mode"] for x in recs)),
        "with_subquery_steps": sum(1 for x in recs if len(x["evidence"]["required"]["steps"]) > 1),
        "multi_table_without_any_link": sum(
            1 for x in recs if len(x["evidence"]["required"]["tables"]) > 1
            and not x["evidence"]["required"]["join_keys"]
            and not x["evidence"]["required"]["implicit_joins"]
            and not x["evidence"]["required"]["set_relations"]),
    }
    json.dump(summ, open(a.summary, "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
