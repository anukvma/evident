"""Agreement between two annotators on the evidence labels, and how often people accept the automatic draft.

  python eval/annotator_agreement.py [--a data/annotation/sheet_annotatorA.csv] [--b ...B.csv]
Reports per field: n, percent agreement, Cohen's kappa; and for each annotator the share of 'yes' on the *_ok fields (an estimate of
the precision of the automatic labels). Lists disagreements for adjudication. Writes reports/annotation_agreement.json.
"""
import argparse
import collections
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIELDS = ["sql_correct", "tables_ok", "columns_ok", "joins_ok", "rowmode_ok", "alt_route_possible"]


def kappa(x, y):
    n = len(x)
    if n == 0:
        return None
    po = sum(a == b for a, b in zip(x, y)) / n
    cx, cy = collections.Counter(x), collections.Counter(y)
    pe = sum(cx[k] * cy[k] for k in set(cx) | set(cy)) / n ** 2
    return None if pe == 1 else (po - pe) / (1 - pe)


def load(p):
    return {r["qid"]: r for r in csv.DictReader(open(p, newline=""))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default=str(ROOT / "data/annotation/sheet_annotatorA.csv"))
    ap.add_argument("--b", default=str(ROOT / "data/annotation/sheet_annotatorB.csv"))
    a = ap.parse_args()
    A, B = load(a.a), load(a.b)
    common = sorted(set(A) & set(B))
    rep = {"questions_in_both": len(common), "fields": {}, "auto_label_accepted_share": {}}
    disagree = []
    for f in FIELDS:
        pairs = [(A[q][f].strip().lower(), B[q][f].strip().lower()) for q in common
                 if A[q][f].strip() and B[q][f].strip()]
        x, y = [p[0] for p in pairs], [p[1] for p in pairs]
        rep["fields"][f] = {"n": len(pairs), "percent_agreement": round(sum(i == j for i, j in pairs) / len(pairs), 3) if pairs else None,
                            "cohens_kappa": None if kappa(x, y) is None else round(kappa(x, y), 3)}
        for q in common:
            if A[q][f].strip() and B[q][f].strip() and A[q][f].strip().lower() != B[q][f].strip().lower():
                disagree.append((q, f, A[q][f], B[q][f]))
    for name, S in (("A", A), ("B", B)):
        rep["auto_label_accepted_share"][name] = {
            f: round(sum(S[q][f].strip().lower() == "yes" for q in common) / max(1, sum(bool(S[q][f].strip()) for q in common)), 3)
            for f in ("tables_ok", "columns_ok", "joins_ok", "rowmode_ok")}
    json.dump(rep, open(ROOT / "reports/annotation_agreement.json", "w"), indent=1)
    print(json.dumps(rep, indent=1))
    print(f"\n{len(disagree)} disagreements to adjudicate:")
    for q, f, x, y in disagree:
        print(f"  {q:16} {f:18} A={x!r:12} B={y!r}")


if __name__ == "__main__":
    main()
