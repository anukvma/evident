"""Baseline plumbing tests with offline mock LLMs (no network, no credentials).
Run: python tests/test_baselines.py [N_questions]   (default 15; levels 0 and 100)"""
import json
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

N = int(sys.argv[1]) if len(sys.argv) > 1 else 15
OUT = ROOT / "data" / "predictions_test_bl"
ok = True


def check(name, cond, detail=""):
    global ok
    print(("PASS" if cond else "FAIL"), name, detail)
    ok &= bool(cond)


def run(system, model):
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f"{system}__{model.replace(':', '_')}.jsonl"
    f.unlink(missing_ok=True)
    subprocess.run([sys.executable, str(ROOT / "baselines" / "run_baseline.py"), "--system", system, "--model", model,
                    "--limit", str(N), "--levels", "0", "100", "--out", str(f), "--workers", "2"],
                   check=True, capture_output=True)
    gold = load_gold(N)
    rows = [score(gold[json.loads(l)["qid"]], Prediction.from_json(json.loads(l)), json.loads(l)["level"])
            for l in open(f)]
    return rows


mean = lambda rows, m: A.summarise(rows, [m], with_ci=False)[m]["mean"]  # noqa: E731

o = run("oracle_schema_t2s", "mock:gold")
check("oracle_schema + gold-SQL LLM: exec_accuracy == 1", mean(o, "exec_accuracy") == 1.0)
check("  prompt contains exactly the required tables", mean(o, "table_recall_selected") == 1.0
      and mean(o, "table_precision_selected") == 1.0)
check("  SQL-derived evidence reproduces gold: column_recall == 1", mean(o, "column_recall") == 1.0)
check("  SQL-derived evidence reproduces gold: row_recall == 1", mean(o, "row_recall") == 1.0, f"(got {mean(o,'row_recall')})")
check("  SQL-derived rows are sufficient", mean(o, "evidence_sufficient") == 1.0)
check("  same at 100 decoys (decoys do not leak into oracle schema)",
      mean([r for r in o if r["level"] == 100], "exec_accuracy") == 1.0)

b = run("oracle_schema_t2s", "mock:bad")
check("broken SQL -> sql_failure", {r["failure"] for r in b} == {"sql_failure"}, f"(got {set(r['failure'] for r in b)})")

w = run("oracle_schema_t2s", "mock:wrong")
fails = {r["failure"] for r in w}
check("valid-but-wrong SQL -> attributed to SQL planning, not retrieval",
      fails <= {"sql_missed_column", "sql_missed_join_key", "sql_incomplete_evidence", "wrong_answer_despite_evidence"},
      f"(got {fails})")

bm = run("bm25_t2s", "mock:gold")
check("bm25 + gold-SQL LLM: exec_accuracy == 1 (answer path independent of retrieval)", mean(bm, "exec_accuracy") == 1.0)
r0 = mean([r for r in bm if r["level"] == 0], "table_recall@5")
r100 = mean([r for r in bm if r["level"] == 100], "table_recall@5")
check("bm25 ranks real tables above decoys better than chance", r100 > 0.3, f"(recall@5 L0 {r0:.2f}, L100 {r100:.2f})")
check("bm25 selected_tables has exactly top-5 (or fewer if db smaller)", all(r["n_selected_tables"] <= 5 for r in bm))

fs = run("fullschema_t2s", "mock:gold")
check("fullschema + gold-SQL LLM: exec_accuracy == 1", mean(fs, "exec_accuracy") == 1.0)
check("fullschema sees every candidate table (recall 1, tiny precision at 100)",
      mean(fs, "table_recall_selected") >= 0.95 and mean([r for r in fs if r["level"] == 100], "table_precision_selected") < 0.3,
      f"(recall {mean(fs,'table_recall_selected'):.2f})")

# retrieve-and-read (no SQL): value-level scoring, evidence measured on the rows placed in the prompt
rr = run("retrieve_read", "mock:gold_values")
check("retrieve_read + gold-values LLM: value_accuracy == 1", mean(rr, "value_accuracy") == 1.0)
check("  strict exec_accuracy is unavailable (NaN) for a value-only answer", all(math.isnan(r["exec_accuracy"]) for r in rr))
check("  answer counted correct via value metric", {r["failure"] for r in rr} == {"correct"})
check("  evidence metrics come from the rows in the prompt (not NaN)", all(not math.isnan(r["row_recall"]) for r in rr))
check("  at most 3 tables read, <= 30 rows per large table",
      all(r["n_selected_tables"] <= 3 for r in rr))
check("  row-level sufficiency is NOT assumed: top-k rows cannot cover complete-set questions",
      mean(rr, "evidence_sufficient") < 1.0, f"(got {mean(rr, 'evidence_sufficient'):.2f})")

# question-blind
subprocess.run([sys.executable, str(ROOT / "baselines" / "run_baseline.py"), "--system", "question_blind",
                "--limit", "200", "--levels", "0", "100", "--out", str(OUT / "qb.jsonl")], check=True, capture_output=True)
gold = load_gold()
qb = [score(gold[json.loads(l)["qid"]], Prediction.from_json(json.loads(l)), json.loads(l)["level"]) for l in open(OUT / "qb.jsonl")]
check("question_blind: retrieval-only (answer metrics NaN, retrieval metrics present)",
      all(r["exec_status"] == "retrieval_only" and math.isnan(r["value_accuracy"]) for r in qb)
      and not math.isnan(qb[0]["table_recall@10"]))
check("question_blind: failure label retrieval_only", {r["failure"] for r in qb} == {"retrieval_only"})
print(f"  question_blind table_recall@5: L0 {mean([r for r in qb if r['level']==0],'table_recall@5'):.3f}"
      f"  L100 {mean([r for r in qb if r['level']==100],'table_recall@5'):.3f}")
print("\nALL PASSED" if ok else "\nFAILURES PRESENT")
sys.exit(0 if ok else 1)
