"""Step 4: materialise row evidence for the pilot and verify it is SUFFICIENT.

For every step with an `evidence_sql` we run it on the source database and collect the rowids it
returns per base table (the rows that pass that step's joins and filters).

Sufficiency check: build a database holding ONLY those rows (same DDL), re-run the gold SQL on
it, and require the same answer (compared as a multiset; ORDER BY queries also compare order).
If it fails, the evidence is incomplete and the record is flagged. Necessity/minimality is NOT
checked here, so these are sufficient row sets, not guaranteed minimal ones.

Row sets above `max_inline_rows` keep only counts + hash + the defining SQL (the SQL is the spec).

Questions whose evidence exceeds --max-replay-rows rows are not replayed (sufficient = "too_large"); build/12 drops them.

Usage: python build/04_materialise_rows.py [--input data/v1/test_evidence_draft.jsonl --out data/v1/test_evidence_rows.jsonl --summary reports/v1/test_evidence_rows_summary.json --max-replay-rows 500000]
Defaults are the pilot files (no cap).
Output: data/evidence_rows.jsonl, reports/evidence_rows_summary.json
"""
import argparse
import collections
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402
from reltable.minidb import build_mini_db, lenient  # noqa: E402

CFG = {"timeout_s": 120, "max_inline_rows": 5000}


def connect(path, timeout):
    con = lenient(sqlite3.connect(f"file:{path}?mode=ro", uri=True))
    deadline = time.time() + timeout
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 200000)
    return con


def canon(rows, ordered):
    keyed = [repr(r) for r in rows]
    return keyed if ordered else sorted(keyed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(paths.DATA / "evidence_draft.jsonl"))
    ap.add_argument("--out", default=str(paths.DATA / "evidence_rows.jsonl"))
    ap.add_argument("--summary", default=str(paths.REPORTS / "evidence_rows_summary.json"))
    ap.add_argument("--max-replay-rows", type=int, default=None)
    a = ap.parse_args()
    out, tally, sizes = [], collections.Counter(), []
    for line in open(a.input):
        r = json.loads(line)
        db = paths.sqlite_path(r["source"], r["db_id"])
        req = r["evidence"]["required"]
        con = connect(db, CFG["timeout_s"])
        steps_out, ids_by_table, err = [], collections.defaultdict(set), None
        for st in req["steps"]:
            if not st["evidence_sql"]:
                steps_out.append({"id": st["id"], "n_combinations": None, "tables": {}}); continue
            try:
                if a.max_replay_rows:            # count first, so a 15M-row complete set is never loaded into memory
                    n_comb = con.execute(f"select count(*) from ({st['evidence_sql']})").fetchone()[0]
                    if n_comb > a.max_replay_rows:
                        err = f"too_large: {st['id']} has {n_comb} evidence combinations"; break
                rows = con.execute(st["evidence_sql"]).fetchall()
            except sqlite3.Error as e:
                err = f"{st['id']}: {e}"; break
            per = collections.defaultdict(set)
            for row in rows:
                for tname, rid in zip(st["evidence_tables"], row):
                    if rid is not None:
                        per[tname].add(rid)
            tinfo = {}
            for tname, ids in per.items():
                srt = sorted(ids)
                tinfo[tname] = {"n_rows": len(srt),
                                "sha1": hashlib.sha1(repr(srt).encode()).hexdigest(),
                                "rowids": srt if len(srt) <= CFG["max_inline_rows"] else None}
                ids_by_table[tname] |= ids
            steps_out.append({"id": st["id"], "n_combinations": len(rows), "tables": tinfo,
                              "rows_mode": st["rows_mode"], "note": st.get("evidence_note")})
        result = {"qid": r["qid"], "steps": steps_out,
                  "total_rows": {t: len(v) for t, v in ids_by_table.items()}}
        # ---- sufficiency
        if err:
            result["sufficient"], result["detail"] = ("too_large" if err.startswith("too_large") else "error"), err
        elif a.max_replay_rows and sum(result["total_rows"].values()) > a.max_replay_rows:
            result["sufficient"], result["detail"] = "too_large", f"{sum(result['total_rows'].values())} evidence rows"
        else:
            try:
                ordered = "order by" in r["gold_sql"].lower()
                want = canon(con.execute(r["gold_sql"]).fetchall(), ordered)
                mini = build_mini_db(db, req["tables"], ids_by_table)
                got = canon(mini.execute(r["gold_sql"]).fetchall(), ordered)
                result["sufficient"] = got == want
                if got != want:
                    result["detail"] = f"gold rows {len(want)} vs evidence-db rows {len(got)}"
                else:
                    # necessity probe: emptying a required table should change the answer
                    unneeded = []
                    for t in req["tables"]:
                        if not ids_by_table.get(t):
                            continue
                        mini.execute("SAVEPOINT probe")
                        mini.execute(f'DELETE FROM main."{t}"')
                        try:
                            same = canon(mini.execute(r["gold_sql"]).fetchall(), ordered) == want
                        except sqlite3.Error:
                            same = False                 # query breaks without it -> needed
                        mini.execute("ROLLBACK TO probe")
                        mini.execute("RELEASE probe")
                        if same:
                            unneeded.append(t)
                    result["tables_not_necessary"] = unneeded
                mini.close()
            except sqlite3.Error as e:
                result["sufficient"], result["detail"] = "error", str(e)[:150]
        con.close()
        tally[str(result["sufficient"])] += 1
        sizes.append(sum(result["total_rows"].values()))
        out.append(result)
        print(r["qid"], result["sufficient"], sum(result["total_rows"].values()), flush=True)
    with open(a.out, "w") as f:
        for o in out:
            f.write(json.dumps(o) + "\n")
    srt = sorted(sizes)
    summ = {"n": len(out), "sufficient": dict(tally),
            "evidence_rows_total": {"median": srt[len(srt) // 2], "p90": srt[int(len(srt) * .9)],
                                    "max": srt[-1]},
            "records_with_unnecessary_table": sum(1 for o in out if o.get("tables_not_necessary")),
            "unnecessary_table_examples": [{"qid": o["qid"], "tables": o["tables_not_necessary"]}
                                           for o in out if o.get("tables_not_necessary")][:15],
            "failures": [{"qid": o["qid"], "detail": o.get("detail")} for o in out if o["sufficient"] is not True]}
    json.dump(summ, open(a.summary, "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
