"""Step 12 (v1): finalise each split from its candidates once evidence exists (build/03, build/04).

A candidate is valid when its row evidence reproduces the gold answer (sufficient == True); too_large / insufficient /
error candidates are dropped. test: all pilot questions plus, per origin, the first (quota - pilot) valid candidates in
pick order; dev: the first dev_sample valid candidates; train: every valid candidate. The evidence files are filtered to
the kept questions so all three files of a split always describe the same questions.

Usage: python build/12_finalise_v1.py [--splits test dev train] [--smoke]
Output: data/v1/{split}.jsonl, data/v1/{split}_evidence_draft.jsonl, data/v1/{split}_evidence_rows.jsonl,
        reports/v1/finalise_summary.json
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reltable import paths  # noqa: E402

CFG = json.load(open(paths.ROOT / "configs" / "v1.json"))


def load(p):
    return [json.loads(l) for l in open(p)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", nargs="+", default=["test", "dev", "train"])
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    d = paths.DATA_V1 / "smoke" if a.smoke else paths.DATA_V1
    if a.smoke:
        CFG["test_quota"] = {k: v // 10 for k, v in CFG["test_quota"].items()}
        CFG["dev_sample"] = 15
    summ = {}
    for s in a.splits:
        cands = load(d / f"{s}_candidates.jsonl")
        draft = {r["qid"]: r for r in load(d / f"{s}_evidence_draft.jsonl")}
        rows = {r["qid"]: r for r in load(d / f"{s}_evidence_rows.jsonl")}
        status = {q: (rows[q]["sufficient"] if q in rows else "no_evidence") for q in (c["qid"] for c in cands)}
        valid = [c for c in cands if status[c["qid"]] is True]
        if s == "test":
            pilot = [c for c in cands if c.get("in_pilot")]
            bad_pilot = [c["qid"] for c in pilot if status[c["qid"]] is not True]
            assert not bad_pilot, f"pilot questions lost their evidence: {bad_pilot}"
            keep = list(pilot)
            for org, quota in CFG["test_quota"].items():
                need = quota - sum(c["origin"] == org for c in pilot)
                pool = sorted((c for c in valid if c["origin"] == org and not c.get("in_pilot")), key=lambda c: c["pick_rank"])
                if len(pool) < need:
                    print(f"WARNING test/{org}: only {len(pool)} valid candidates for {need} places", flush=True)
                keep += pool[:need]
        elif s == "dev":
            keep = sorted(valid, key=lambda c: c["pick_rank"])[: CFG["dev_sample"]]
        else:
            keep = valid
        ids = {c["qid"] for c in keep}
        for name, recs in ((f"{s}.jsonl", keep), (f"{s}_evidence_draft.jsonl", [draft[q["qid"]] for q in keep]),
                           (f"{s}_evidence_rows.jsonl", [rows[q["qid"]] for q in keep])):
            with open(d / name, "w") as f:
                for r in recs:
                    f.write(json.dumps(r, default=str) + "\n")
        summ[s] = {
            "candidates": len(cands), "kept": len(keep),
            "candidate_status": collections.Counter(str(v) for v in status.values()),
            "kept_by_origin": collections.Counter(c["origin"] for c in keep),
            "kept_databases": len({(c["source"], c["db_id"]) for c in keep}),
            "kept_needs_review": sum(bool(draft[q]["needs_review"]) for q in ids if "needs_review" in draft[q]),
        }
        print(s, json.dumps(summ[s], default=str), flush=True)
    out = d / "finalise_summary.json" if a.smoke else paths.REPORTS / "v1" / "finalise_summary.json"
    json.dump(json.loads(json.dumps(summ, default=str)), open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
