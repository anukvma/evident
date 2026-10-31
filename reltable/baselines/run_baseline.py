"""Run a baseline over the pilot and write a prediction file for eval/run_eval.py.

  python baselines/run_baseline.py --system question_blind --out data/predictions/question_blind.jsonl
  python baselines/run_baseline.py --system bm25_t2s --model gemini-2.5-flash --out data/predictions/bm25_t2s.jsonl
  python baselines/run_baseline.py --system fullschema_t2s --model gemini-2.5-flash --dry-run     # sizes only, no LLM

Systems: question_blind | oracle_schema_t2s | bm25_t2s | fullschema_t2s | dense_t2s | hybrid_t2s | graph_t2s |
         retrieve_read (no SQL; --table-retriever, --row-retriever, --read-tables x --rows-per-table set its variant)
Dataset: DREDGE_SPLIT=pilot|test|dev|train and DREDGE_TIER=random|hard (environment) choose questions and decoys.
Re-running with the same --out resumes (finished (qid, level) lines are kept). LLM responses are cached.
"""
import argparse
import json
import statistics as st
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baselines import question_blind, retrieve_read, text2sql  # noqa: E402
from reltable.evaluation import variants  # noqa: E402
from reltable.evaluation.gold import load_gold  # noqa: E402
from reltable.evaluation.schema import DEFAULT_LEVELS  # noqa: E402
from reltable.llm import LLM  # noqa: E402


def done_keys(path):
    keys = set()
    if Path(path).exists():
        for line in open(path):
            if line.strip():
                d = json.loads(line)
                if not d.get("llm_error"):
                    keys.add((d["qid"], d["level"]))
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", required=True, choices=["question_blind", "retrieve_read", *text2sql.SYSTEMS])
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--out", default=None)
    ap.add_argument("--levels", nargs="+", type=int, default=list(DEFAULT_LEVELS))
    ap.add_argument("--limit", type=int, default=None, help="first N pilot questions")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--samples", type=int, default=3, help="sample rows per table in the prompt")
    ap.add_argument("--max-prompt-chars", type=int, default=300_000)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--read-tables", type=int, default=3, help="retrieve_read: tables whose rows are read")
    ap.add_argument("--rows-per-table", type=int, default=30, help="retrieve_read: rows read per table")
    ap.add_argument("--table-retriever", default="bm25", choices=["bm25", "hybrid"], help="retrieve_read: table retrieval")
    ap.add_argument("--row-retriever", default="fts", choices=["fts", "dense", "hybrid"], help="retrieve_read: row retrieval")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-derive", action="store_true", help="skip SQL-derived evidence")
    ap.add_argument("--no-cache", action="store_true", help="call the model again instead of reusing cached responses")
    ap.add_argument("--run-id", default=None, help="label a repeat run (e.g. r2); appended to the system name as '#r2' so "
                    "repeats can be scored and compared. Repeats need --no-cache, otherwise they replay the first run.")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.system == "question_blind":
        n = question_blind.run(a.out, tuple(a.levels), a.limit)
        print(f"wrote {n} predictions to {a.out}")
        return

    gold = load_gold(a.limit)
    llm = LLM(a.model, max_tokens=a.max_tokens, use_cache=not a.no_cache)
    todo = [(g, l) for g in gold.values() for l in a.levels if variants.level_available(g.qid, l)]
    if not a.dry_run:
        if not a.out:
            ap.error("--out is required unless --dry-run")
        skip = done_keys(a.out)
        todo = [(g, l) for g, l in todo if (g.qid, l) not in skip]
        print(f"{len(todo)} to run ({len(skip)} already done)", flush=True)

    kw = dict(topk=a.topk, n_sample_rows=a.samples, max_prompt_chars=a.max_prompt_chars,
              derive=not a.no_derive, dry_run=a.dry_run)
    lock, results, f = threading.Lock(), [], None
    if not a.dry_run:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        f = open(a.out, "a")

    if a.run_id and not a.no_cache and not a.dry_run:
        ap.error("--run-id needs --no-cache: with the cache on, a repeat just replays the first run's responses")

    def work(g, l):
        if a.system == "retrieve_read":
            rec = retrieve_read.predict(g, l, llm, read_tables=a.read_tables, rows_per_table=a.rows_per_table,
                                        dry_run=a.dry_run, table_retriever=a.table_retriever, row_retriever=a.row_retriever)
        else:
            rec = text2sql.predict(a.system, g, l, llm, **kw)
        if a.run_id:
            rec["system"] = f"{rec['system']}#{a.run_id}"
        return rec

    with ThreadPoolExecutor(max_workers=1 if a.dry_run else a.workers) as ex:
        futs = {ex.submit(work, g, l): (g.qid, l) for g, l in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                rec = fu.result()
            except Exception as e:                       # noqa: BLE001  one bad question must not kill the run
                q, l = futs[fu]
                rec = {"system": f"{a.system}[{a.model}]", "qid": q, "level": l, "abstained": True,
                       "llm_error": f"{type(e).__name__}: {str(e)[:200]}"}
            with lock:
                results.append(rec)
                if f:
                    f.write(json.dumps(rec) + "\n"); f.flush()
            if i % 20 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}", flush=True)
    if f:
        f.close()

    if a.dry_run:
        print(f"\nDRY RUN  system={a.system}  model={a.model}  questions={len(gold)}  levels={a.levels}")
        total = 0
        for l in a.levels:
            ch = [r["prompt_chars"] for r in results if r["level"] == l]
            tr = sum(1 for r in results if r["level"] == l and r["truncated_tables"])
            total += sum(ch)
            print(f"  level {l:>3}: prompts={len(ch)}  chars median={st.median(ch):,.0f} p95="
                  f"{sorted(ch)[int(len(ch) * .95)]:,.0f} max={max(ch):,.0f}  ~tokens median={st.median(ch) // 4:,.0f}"
                  f"  truncated={tr}")
        print(f"  TOTAL calls={len(results)}  est. input tokens ~{total // 4:,}")
    else:
        errs = sum(1 for r in results if r.get("llm_error"))
        print(f"done: {len(results)} predictions, {errs} with LLM errors -> {a.out}")


if __name__ == "__main__":
    main()
