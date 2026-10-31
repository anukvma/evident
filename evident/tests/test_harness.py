"""Harness self-test: trivial systems must produce their exact expected scores.
Run: python tests/test_harness.py [N_questions]   (default 40; levels 0 and 100)."""
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evident.evaluation import aggregate as A  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402
from evident.evaluation.metrics import score  # noqa: E402
from evident.evaluation.schema import Prediction  # noqa: E402
import json  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
LEVELS = [0, 100]
PRED = ROOT / "data" / "predictions_test"


def main():
    subprocess.run([sys.executable, str(ROOT / "baselines" / "trivial.py"), "--out", str(PRED),
                    "--limit", str(N), "--levels", *map(str, LEVELS)], check=True, capture_output=True)
    gold = load_gold(N)
    res = {}
    for f in sorted(PRED.glob("*.jsonl")):
        rows = []
        for line in open(f):
            p = Prediction.from_json(json.loads(line))
            rows.append(score(gold[p.qid], p, p.level))
        res[f.stem] = rows
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name, detail)
        ok &= bool(cond)

    mean = lambda rows, m: A.summarise(rows, [m], with_ci=False)[m]["mean"]  # noqa: E731
    o = res["oracle"]
    for m in ("exec_accuracy", "table_recall@10", "table_recall@50", "table_full_coverage@10", "table_recall_selected", "table_precision_selected",
              "column_recall", "column_precision", "row_recall", "row_precision", "row_complete_set",
              "evidence_sufficient"):
        check(f"oracle {m} == 1", mean(o, m) == 1.0, f"(got {mean(o, m)})")
    ceil = sum(min(1, 1 / len(g.required_tables)) for g in gold.values()) / len(gold)
    r1 = mean([r for r in o if r["level"] == 0], "table_recall@1")
    check("oracle table_recall@1 equals its ceiling mean(1/|required|)", abs(r1 - ceil) < 1e-9, f"(got {r1:.4f}, ceiling {ceil:.4f})")
    check("oracle join_key_recall == 1", mean(o, "join_key_recall") == 1.0)
    check("oracle all 'correct'", all(r["failure"] == "correct" for r in o))
    check("oracle exec_accuracy_set == 1 and >= exec_accuracy", mean(o, "exec_accuracy_set") == 1.0)
    check("oracle evidence_sufficient_eq == evidence_sufficient == 1",
          mean(o, "evidence_sufficient_eq") == 1.0 and mean(o, "evidence_sufficient") == 1.0)
    n = mean(res["noop"], "exec_accuracy")
    check("noop exec_accuracy == 0", n == 0.0)
    check("noop retrieval metrics are NaN not 0", all(math.isnan(r["table_recall@10"]) for r in res["noop"]))
    check("noop failure == no_answer", {r["failure"] for r in res["noop"]} == {"no_answer"})
    rt = res["random_tables"]
    check("random_tables is retrieval-only: answer metrics are NaN (not 0)",
          all(math.isnan(r["exec_accuracy"]) and math.isnan(r["value_accuracy"]) for r in rt))
    check("random_tables failure label == retrieval_only", {r["failure"] for r in rt} == {"retrieval_only"})
    r100 = mean([r for r in rt if r["level"] == 100], "table_recall@10")
    r0 = mean([r for r in rt if r["level"] == 0], "table_recall@10")
    check("random retrieval is worse with decoys", r100 < r0, f"(L0 {r0:.2f} -> L100 {r100:.2f})")
    ar = res["all_rows"]
    check("all_rows: exec correct", mean(ar, "exec_accuracy") == 1.0)
    check("all_rows: row_recall == 1", mean(ar, "row_recall") == 1.0)
    check("all_rows: row_precision < 1", mean(ar, "row_precision") < 1.0, f"(got {mean(ar, 'row_precision'):.3f})")
    check("all_rows: sufficient", mean(ar, "evidence_sufficient") == 1.0)
    for name, want in (("miss_table", "missed_table"), ("sql_error", "sql_failure"),
                       ("wrong_answer", "wrong_answer_despite_evidence"),
                       ("incomplete_rows", "incomplete_evidence")):
        got = {r["failure"] for r in res[name]}
        check(f"{name} -> {want}", got == {want}, f"(got {got})")
    # level bookkeeping
    check("candidate table count grows by exactly the level",
          all(r["n_candidate_tables"] > 0 for r in o) and
          {r["level"] for r in o} == set(LEVELS))
    # missing predictions count as wrong
    g = next(iter(gold.values()))
    m = score(g, Prediction.empty("x", g.qid, 0), 0)
    check("missing prediction -> exec 0 and failure no_answer", m["exec_accuracy"] == 0 and m["failure"] == "no_answer")
    # --- equivalence-aware sufficiency: only a CONFIRMED alternative reference can rescue strict insufficiency
    from evident.evaluation import alt_refs
    from evident.evaluation.metrics import row_metrics
    allg = load_gold()
    g = allg["bird-113"]                     # gold joins loan-account-disp-client; a valid shortcut skips `account`
    rows_no_account = {t: sorted(v) for t, v in g.row_ids.items() if t.lower() != "account"}
    alt_sql = ("SELECT T3.birth_date FROM loan AS T1 INNER JOIN disp AS T2 ON T1.account_id = T2.account_id "
               "INNER JOIN client AS T3 ON T2.client_id = T3.client_id WHERE T1.amount = 98832 AND T1.date = '1996-01-03'")
    pred = Prediction(system="t", qid="bird-113", level=0, retrieved_rows=rows_no_account)
    saved = alt_refs._cache
    try:
        alt_refs._cache = {}
        m0 = row_metrics(g, pred, 0)
        alt_refs._cache = {"bird-113": {"alternatives": [{"sql": alt_sql, "status": "proposed"}]}}
        m1 = row_metrics(g, pred, 0)
        alt_refs._cache = {"bird-113": {"alternatives": [{"sql": alt_sql, "status": "confirmed"}]}}
        m2 = row_metrics(g, pred, 0)
    finally:
        alt_refs._cache = saved
    check("skipping a bridge table is strictly insufficient", m0["evidence_sufficient"] == 0.0 and m0["evidence_sufficient_eq"] == 0.0)
    check("a merely PROPOSED alternative does not change sufficiency", m1["evidence_sufficient_eq"] == 0.0)
    check("a CONFIRMED alternative makes it sufficient (strict stays 0)", m2["evidence_sufficient_eq"] == 1.0 and m2["evidence_sufficient"] == 0.0)
    print("\nALL PASSED" if ok else "\nFAILURES PRESENT")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
