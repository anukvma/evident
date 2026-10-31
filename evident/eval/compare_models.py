"""Compare two models (consolidated runs) on the levels they share. NO scoring happens here.

  python eval/compare_models.py --a reports/eval_final_v6 --b reports/eval_final_gpt4o --a-name "Gemini 2.5 Flash" --b-name "GPT-4o"

For each SQL system (oracle_schema_t2s, bm25_t2s, fullschema_t2s) and each common level: value accuracy per model; the scale effect
(change from level 0 to the highest common level, paired by question, database-cluster bootstrap CI); refusals; whether the two models rank the
systems in the same order; and the paired model difference (B - A) with CI. Answers: does the pattern found with model A hold for model B?
"""
import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SQLSYS = ["oracle_schema_t2s", "fullschema_t2s", "bm25_t2s"]


def load(d):
    rows = [json.loads(l) for l in open(Path(d) / "per_question.jsonl")]
    for r in rows:
        r["sys"] = r["system"].split("[")[0]
    return rows


def ok(x):
    return isinstance(x, float) and not math.isnan(x)


def boot(diffs, B=2000, seed=0):
    """diffs: list of (db, value). Mean and 95% CI resampling databases."""
    by = {}
    for db, v in diffs:
        by.setdefault(db, []).append(v)
    dbs, rng, bs = list(by), random.Random(seed), []
    for _ in range(B):
        samp = [x for d in (rng.choice(dbs) for _ in dbs) for x in by[d]]
        bs.append(sum(samp) / len(samp))
    bs.sort()
    allv = [v for _, v in diffs]
    return sum(allv) / len(allv), bs[int(.025 * B)], bs[int(.975 * B) - 1], len(allv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True), ap.add_argument("--b", required=True)
    ap.add_argument("--a-name", default="A"), ap.add_argument("--b-name", default="B")
    ap.add_argument("--out", default=None, help="write the numbers as JSON (used by make_paper_tables.py)")
    a = ap.parse_args()
    A, B = load(a.a), load(a.b)
    common = sorted({r["level"] for r in A} & {r["level"] for r in B})
    top = max(common)
    print(f"common levels: {common}   (compared at +0 vs +{top})\n")
    res = {"common_levels": common, "top": top, "systems": {}}
    mean = lambda rows, m: (sum(r[m] for r in rows if ok(r[m])) / max(1, sum(ok(r[m]) for r in rows)))  # noqa: E731
    print(f"{'system':20} {'model':18} " + " ".join(f"+{l:<4}" for l in common) + f"  {'delta(+%d)'%top:>30}  refusals")
    for s in SQLSYS:
        res["systems"][s] = {}
        for name, R in ((a.a_name, A), (a.b_name, B)):
            rs = [r for r in R if r["sys"] == s and r["level"] in common]
            lv = {l: mean([r for r in rs if r["level"] == l], "value_accuracy") for l in common}
            by0 = {r["qid"]: r["value_accuracy"] for r in rs if r["level"] == 0 and ok(r["value_accuracy"])}
            dd = [(r["db"], r["value_accuracy"] - by0[r["qid"]]) for r in rs
                  if r["level"] == top and ok(r["value_accuracy"]) and r["qid"] in by0]
            m, lo, hi, n = boot(dd)
            ref = sum(1 for r in rs if r["exec_status"] == "no_answer") / len(rs)
            suff = mean(rs, "evidence_sufficient"); eq = mean(rs, "evidence_sufficient_eq")
            overall = mean(rs, "value_accuracy")
            res["systems"][s][name] = {"by_level": lv, "delta": [m, lo, hi], "n_delta": n, "refusal_share": ref,
                                       "overall": overall, "evid_suff": suff, "evid_suff_eq": eq}
            print(f"{s:20} {name:18} " + " ".join(f"{lv[l]:.3f}" for l in common) + f"  {m:+.3f} [{lo:+.3f},{hi:+.3f}]  {100 * ref:5.1f}%")
    # ranking agreement
    order = {n: sorted(SQLSYS, key=lambda s: -res["systems"][s][n]["overall"]) for n in (a.a_name, a.b_name)}
    res["same_order"] = order[a.a_name] == order[a.b_name]
    res["order"] = order
    print(f"\nsystem ranking by overall value accuracy:  {a.a_name}: {order[a.a_name]}\n{' ' * 43}{a.b_name}: {order[a.b_name]}   -> same order: {res['same_order']}")
    # paired model difference (B - A) per system, over common levels
    print(f"\npaired difference in value accuracy ({a.b_name} minus {a.a_name}), all common levels, same questions:")
    res["model_diff"] = {}
    for s in SQLSYS:
        ia = {(r["qid"], r["level"]): r["value_accuracy"] for r in A if r["sys"] == s and r["level"] in common and ok(r["value_accuracy"])}
        dd = [(r["db"], r["value_accuracy"] - ia[(r["qid"], r["level"])]) for r in B
              if r["sys"] == s and (r["qid"], r["level"]) in ia and ok(r["value_accuracy"])]
        m, lo, hi, n = boot(dd)
        res["model_diff"][s] = [m, lo, hi]
        print(f"  {s:20} {m:+.3f}  [{lo:+.3f}, {hi:+.3f}]  (n={n})")
    # does the qualitative pattern replicate?
    pat = {}
    for n in (a.a_name, a.b_name):
        bm = res["systems"]["bm25_t2s"][n]["delta"]; fs = res["systems"]["fullschema_t2s"][n]["delta"]
        pat[n] = {"bm25_declines": bm[2] < 0, "fullschema_flat": fs[1] <= 0 <= fs[2]}
    res["pattern"] = pat
    print("\nqualitative pattern at the highest common level:")
    for n, p in pat.items():
        print(f"  {n:18} BM25 pipeline declines significantly: {p['bm25_declines']} | full-schema change is not significant: {p['fullschema_flat']}")
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1, default=str)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
