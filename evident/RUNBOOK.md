# RUNBOOK — running the pilot evaluation

All commands run from `Stride_dataset/evident/` with the project venv (has sqlglot, sklearn, requests):

```bash
cd /Users/anupreksha/personal/research/stride/Stride_dataset/evident
source ../../.venv/bin/activate
```

## 0. Sanity (no network, no credentials, ~15 min total)
```bash
python tests/test_harness.py 40     # 30 checks on the scorer, expect "ALL PASSED"
python tests/test_baselines.py 15   # 15 checks on baselines with mock LLMs, expect "ALL PASSED"
```

## 1. Free baselines (no LLM)
```bash
mkdir -p data/predictions
python baselines/run_baseline.py --system question_blind --out data/predictions/question_blind.jsonl
python baselines/trivial.py --out data/predictions      # optional: reference systems (oracle, noop, ...)
```

## 2. Authenticate (same mechanism as STRIDE's `retrieval/llm_client.py`)
Run these yourself (they need your Apple identity). In Claude Code prefix with `!`.
```bash
# Endor models, e.g. gemini-2.5-flash, endor-text-qwen3-8b-latest
export ENDOR_TOKEN=$(appleconnect getToken -I 199323 -t oauth -C qszau5myzgukyu8sybqajz7u9chjdg -G pkce | grep -v oauth-id-token | sed 's/oauth-access-token: //')

# Floodgate models, e.g. openai:gpt-4o, aws:anthropic..., gemini:...
export TOKEN=$(appleconnect getToken -t oauth -G pkce -C hvys3fcwcteqrvw3qzkvtk86viuoqv -o openid,dsid,accountname,email,groups --interactivity-type none | grep id-token | cut -d ' ' -f 2)
```
Tokens last ~1 h (Floodgate) to ~8 h (Endor); the client re-fetches via `appleconnect` if they are not exported.
If STRIDE lives elsewhere: `export STRIDE_REPO=/path/to/stride`.

## 3. Smoke test with the real model (3 questions, ~3 calls)
```bash
python baselines/run_baseline.py --system oracle_schema_t2s --model gemini-2.5-flash \
       --limit 3 --levels 0 --workers 1 --out data/predictions/smoke.jsonl
python eval/run_eval.py --predictions data/predictions/smoke.jsonl --out reports/eval_smoke --levels 0 --limit 3 --no-ci
python - <<'PY'
import json; d=json.load(open("reports/eval_smoke/summary.json"))["systems"]
for s,v in d.items(): print(s, v["overall"]["exec_accuracy"], v["failure_breakdown"])
PY
```
Look at `data/predictions/smoke.jsonl` (`raw_response`, `sql`, `llm_error`) before spending more.

## 4. Preview cost before each real run (no LLM call)
```bash
python baselines/run_baseline.py --system fullschema_t2s --model gemini-2.5-flash --dry-run
```
Measured for the full pilot (200 questions x 4 levels = 800 calls per system; tokens are chars/4 estimates):

| System | Calls | Est. input tokens | Median prompt, level 0 -> 100 |
|---|---|---|---|
| oracle_schema_t2s | 800 | ~0.37 M | ~340 tokens (flat) |
| bm25_t2s | 800 | ~0.66 M | ~540 -> ~640 tokens |
| fullschema_t2s | 800 | **~4.5 M** | ~730 -> ~12,000 tokens (p95 ~17 k, nothing truncated) |

## 5. Full runs (resumable: re-running the same command skips finished (qid, level) lines)
```bash
M=gemini-2.5-flash
python baselines/run_baseline.py --system oracle_schema_t2s --model $M --workers 4 --out data/predictions/oracle_schema_t2s.$M.jsonl
python baselines/run_baseline.py --system bm25_t2s          --model $M --workers 4 --out data/predictions/bm25_t2s.$M.jsonl
python baselines/run_baseline.py --system fullschema_t2s    --model $M --workers 4 --out data/predictions/fullschema_t2s.$M.jsonl
```
Responses are cached in `data/cache/llm/<model>/`, so reruns cost nothing and are bit-identical.
For repeated trials (variance across LLM sampling) add `--no-cache` and a different `--out`.
Lower `--workers` if you see 429s. Use `--limit 40 --levels 0 100` first for a cheaper dry pass.

### 5b. Non-SQL baseline (retrieve-and-read: no query is written)
```bash
python baselines/run_baseline.py --system retrieve_read --model $M --workers 4 --out data/predictions/retrieve_read.$M.jsonl
#   knobs: --read-tables 3 (tables whose rows are read)  --rows-per-table 30
```
Pipeline: BM25 picks the tables, SQLite FTS5 (BM25) picks rows inside them (a table with <= 30 rows is read in full,
and if keyword search finds nothing the head of the table is shown), then the LLM answers from those rows as a flat JSON list.
The first run builds one persistent FTS index per table it needs (about 6 s for a 184k-row table); build times are written to
`data/cache/fts/manifest.json` and count as preprocessing cost. Do not start two runs of this baseline at the same time while
indexes are still being built.

## 6. Score
```bash
python eval/run_eval.py \
  --predictions data/predictions/question_blind.jsonl data/predictions/oracle_schema_t2s.$M.jsonl \
                data/predictions/bm25_t2s.$M.jsonl data/predictions/fullschema_t2s.$M.jsonl \
  --out reports/eval_pilot_v1
```
Outputs: `reports/eval_pilot_v1/summary.json` (overall, by level/source/operation/n_tables, scale degradation
vs level 0, failure breakdown, cost, paired differences) and `per_question.jsonl`.

## 7. How to read the results
* **Two answer metrics.** `exec_accuracy` is strict (row multiset equals gold; only for systems that return SQL or rows).
  `value_accuracy` compares the flat multiset of cell values (strings case-folded, '1,234' == 1234, column layout and row
  order ignored) and is defined for EVERY system, so SQL and non-SQL systems are comparable on it. Failure labels use
  `exec_accuracy` when available, else `value_accuracy`. `value_accuracy` is looser than `exec_accuracy`: do not mix them
  in one column.
* **oracle_schema_t2s** = LLM SQL ability with perfect retrieval; the ceiling for the other two.
* **bm25_t2s minus oracle_schema_t2s** = what imperfect retrieval costs; **fullschema_t2s** shows what a long
  context costs as decoys grow (watch `scale_degradation_vs_level0`).
* **question_blind** is the shortcut control: if its `table_recall@5` at level 100 approaches a real system's,
  the scale curve is measuring schema shortcuts (measured so far: 0.02 at level 100, i.e. chance).
* Failure labels starting `sql_` mean the SQL itself skipped required evidence (the columns/joins/rows of a
  text-to-SQL system come from its SQL, not from retrieval); `missed_table` is a retrieval miss.
* Token counts are estimates (chars/4); latency excludes retries and auth, as in STRIDE's evaluation.
* Never cite a number from `mock:*` runs.

## 8. Not yet built
Dense and hybrid retrieval (need an embedding endpoint), graph-aware retrieval, T-RAG/TQA-Bench reproductions,
row-scale variants, update track.

---
# PART 2 — closing the evaluation gaps (added after pilot v1)

Everything below is **run by you**; I only ran the offline tests and the data preparation. Order matters: steps 1–2 fix the existing
numbers, 3–6 add new evidence, 7 is human work, 8 hands results back.
```bash
cd /Users/anupreksha/personal/research/stride/Stride_dataset/evident && source ../../.venv/bin/activate
M=gemini-2.5-flash
```

## What changed in the tooling (one line each)
* Decoy levels **250 and 500** now exist (appended; levels <= 100 are byte-identical, so every cached Gemini result is still valid —
  checked: 0 of 75 rebuilt prompts differ). `build/05_make_decoys.py` now refuses to overwrite manifests; never use `--force`.
* New metrics: `exec_accuracy_set` (BIRD-style set equality, for comparability with published numbers) and `evidence_sufficient_eq`
  (equivalence-aware sufficiency, see step 2). Retrieval-only systems (question-blind) now report answer metrics as n/a, not 0.000.
* `eval/run_eval.py --workers N` scores in parallel; default levels are the ones present in the prediction files.
* `--run-id` repeats runs for variance; `eval/variance.py` summarises them. `build/08` + `eval/annotator_agreement.py` for human validation.

## 0. Check the extension (no cost)
```bash
cat reports/decoys_summary_ext.json        # expect 8 checks x 200 = all *_ok, "failures": []
```

## 1. Re-score the existing Gemini runs with the corrected metrics (parallel, ~10 min)
```bash
python baselines/run_baseline.py --system question_blind --levels 0 10 50 100 250 500 --out data/predictions/question_blind.jsonl
mkdir -p reports/eval_parts
python eval/run_eval.py --predictions data/predictions/oracle_schema_t2s.$M.jsonl --levels 0 10 50 100 --workers 6 --out reports/eval_parts/oracle_schema
python eval/run_eval.py --predictions data/predictions/bm25_t2s.$M.jsonl          --levels 0 10 50 100 --workers 6 --out reports/eval_parts/bm25
python eval/run_eval.py --predictions data/predictions/fullschema_t2s.$M.jsonl    --levels 0 10 50 100 --workers 6 --out reports/eval_parts/fullschema
python eval/run_eval.py --predictions data/predictions/question_blind.jsonl       --levels 0 10 50 100 --workers 6 --out reports/eval_parts/question_blind
python eval/consolidate.py --parts reports/eval_parts/* --out reports/eval_final_v2
```
(The first scoring call spends a few minutes recomputing gold-answer hashes for the new set metric; later calls reuse them.)
Expect the old numbers to reproduce, plus the new `exec_accuracy_set` column (about 3–4 points above `exec_accuracy`).

## 2. Equivalence-aware sufficiency (human adjudication, then a 1-minute rescore)
```bash
python build/07_propose_alt_refs.py --per-question reports/eval_final_v2/per_question.jsonl \
       --predictions data/predictions/*.$M.jsonl          # prints 33+ proposals as  [proposed] qid:i  GOLD vs ALT
# READ each pair. Same meaning (e.g. skips a bridge table) -> confirm. Right answer by coincidence or an extra/different
# condition -> reject. Do not confirm by question when its variants differ (use qid:i).
python build/07_propose_alt_refs.py --confirm bird-1258 spider-dev-103 --reject spider-dev-912 --reviewer "<your name>"
python eval/rescore_sufficiency.py --per-question reports/eval_final_v2/per_question.jsonl \
       --predictions data/predictions/*.$M.jsonl --out reports/eval_final_v3
```
Known from my manual review of the oracle cases: bird-113, bird-1526, bird-137, bird-1258, spider-dev-103/104 are valid shortcuts;
spider-dev-912 is a coincidence (likes received vs likes given). Several proposals are new (bm25/fullschema) and unreviewed; one
bird-113 variant adds `type = 'OWNER'`, which is NOT obviously equivalent. Only your confirmations count.

## 3. Higher decoy levels 250 and 500 (the key experiment: where does full-schema prompting break?)
Estimated input tokens (dry run, chars/4): full schema ~28.6k per prompt at 250 (max ~34k) and ~54k at 500 (max ~58k), **~16.2 M in total
for 400 calls**; BM25 and oracle prompts stay under 1k. Nothing is truncated at the default cap.
```bash
python baselines/run_baseline.py --system bm25_t2s       --model $M --levels 250 500 --workers 4 --out data/predictions/bm25_t2s.$M.L250_500.jsonl
python baselines/run_baseline.py --system oracle_schema_t2s --model $M --levels 250 500 --workers 4 --out data/predictions/oracle_schema_t2s.$M.L250_500.jsonl   # ~free: same prompt, cached
python baselines/run_baseline.py --system fullschema_t2s --model $M --levels 250 500 --workers 4 --out data/predictions/fullschema_t2s.$M.L250_500.jsonl
```
Score each system from BOTH its files (same system label, merged by question and level), all six levels:
```bash
python eval/run_eval.py --predictions data/predictions/oracle_schema_t2s.$M.jsonl data/predictions/oracle_schema_t2s.$M.L250_500.jsonl --workers 6 --out reports/eval_parts6/oracle_schema
python eval/run_eval.py --predictions data/predictions/bm25_t2s.$M.jsonl          data/predictions/bm25_t2s.$M.L250_500.jsonl          --workers 6 --out reports/eval_parts6/bm25
python eval/run_eval.py --predictions data/predictions/fullschema_t2s.$M.jsonl    data/predictions/fullschema_t2s.$M.L250_500.jsonl    --workers 6 --out reports/eval_parts6/fullschema
python eval/run_eval.py --predictions data/predictions/question_blind.jsonl       --workers 6 --out reports/eval_parts6/question_blind
python eval/consolidate.py --parts reports/eval_parts6/* --out reports/eval_final_6levels
```
What to look for: does `fullschema_t2s` stay flat at 250/500? If it drops, the story is "scale hurts everything eventually, at different
points"; if it stays flat, "retrieval is the bottleneck, and context cost is the price".

## 4. The non-SQL arm (retrieve-and-read)
```bash
python baselines/run_baseline.py --system retrieve_read --model $M --levels 0 10 50 100 250 500 --workers 4 --out data/predictions/retrieve_read.$M.jsonl
python eval/run_eval.py --predictions data/predictions/retrieve_read.$M.jsonl --workers 6 --out reports/eval_parts6/retrieve_read
```
Its answer metric is `value_accuracy` (exec accuracy is n/a for a text answer). Do not start two retrieve_read runs at once while indexes build.

## 5. A second model (is the effect model-specific?)
```bash
export TOKEN=$(appleconnect getToken -t oauth -G pkce -C hvys3fcwcteqrvw3qzkvtk86viuoqv -o openid,dsid,accountname,email,groups --interactivity-type none | grep id-token | cut -d ' ' -f 2)
M2=openai:gpt-4o ; F=openai_gpt-4o            # any Floodgate id; F is just a filename-safe label
for S in oracle_schema_t2s bm25_t2s fullschema_t2s; do
  python baselines/run_baseline.py --system $S --model $M2 --levels 0 10 50 100 --workers 4 --out data/predictions/$S.$F.jsonl
done
```
Full-schema at 100 decoys is ~12k tokens × 200; add `--levels 250 500` later only if step 3 shows a break for the first model.

## 6. Run-to-run variance (do this for the two systems whose scale effect you will report)
```bash
for R in r2 r3; do
  for S in bm25_t2s fullschema_t2s; do
    python baselines/run_baseline.py --system $S --model $M --levels 0 100 --no-cache --run-id $R --workers 4 --out data/predictions/$S.$M.$R.jsonl
  done
done
python eval/run_eval.py --levels 0 100 --workers 6 --out reports/eval_variance --predictions \
   data/predictions/bm25_t2s.$M.jsonl data/predictions/bm25_t2s.$M.r2.jsonl data/predictions/bm25_t2s.$M.r3.jsonl \
   data/predictions/fullschema_t2s.$M.jsonl data/predictions/fullschema_t2s.$M.r2.jsonl data/predictions/fullschema_t2s.$M.r3.jsonl
python eval/variance.py --per-question reports/eval_variance/per_question.jsonl
```
A scale effect smaller than the between-run spread must not be reported as an effect (the oracle already shows ~3.5% of questions flip).

## 7. Human validation of the evidence labels (two people, independently, ~1–2 hours each)
```bash
python build/08_export_annotation_sheet.py --n 40     # -> data/annotation/sheet_annotatorA.csv, ...B.csv, GUIDE.md
# each annotator fills THEIR OWN copy, without talking to the other; then:
python eval/annotator_agreement.py                     # kappa per field, acceptance rate of the automatic labels, disagreement list
```

## 8. Hand back to me
Send `reports/eval_final_6levels/headline.md` (and the consolidated parts for any other model), `reports/eval_variance` output of
`eval/variance.py`, the adjudication result (`data/alt_reference_sql.json`), and `reports/annotation_agreement.json`.
I then regenerate the paper tables (`python eval/make_paper_tables.py --final <dir> --outdir ../LaTeX2e+Proceedings+Template+ZIP`),
update only what the data supports, and re-run the sanity checks (oracle flat, labels spot-checked, set vs multiset).


---
## Update 2026-10-28 — what happened to Part 2 and what is left
Done (steps 1–4 consolidated into reports/eval_final_v5; adjudication applied from reports/alt_ref_review.csv). Read DESIGN.md "Pilot results v2".
**Interrupted runs:** if a long run stops with `401 Unauthorized`, refresh the token and run the SAME command again with the SAME `--out`: it resumes and
retries only failed calls (failed lines stay in the file; the scorer keeps the last line per question and level). `eval/run_eval.py` and `consolidate.py`
now print a WARNING for any call that still failed (`system_error`): re-run before using those numbers.
**Still to do:** (a) the one failed bm25 call (commands in DESIGN.md); (b) step 5 second model; (c) step 6 variance; (d) step 7 two-annotator validation;
(e) optional: retrieve_read with a larger budget (`--read-tables 5 --rows-per-table 100`) as an ablation, since it ran in one configuration only.

## Update 2026-10-29 — second model done
GPT-4o scored (reports/eval_final_gpt4o) and compared (reports/model_comparison.json); paper Section 7.4 and Table 4 added. To regenerate every paper number:
  python eval/make_paper_tables.py --final reports/eval_final_v6 --second reports/eval_final_gpt4o --first-name "Gemini 2.5 Flash" --second-name "GPT-4o" --outdir ../LaTeX2e+Proceedings+Template+ZIP
Still to do: step 6 variance (repeat runs), step 7 annotation, GPT-4o at +250/+500 if you want the second model to cover the full range (full schema: ~16M tokens), name collision search, TODO notes.


---
# Part 3 — DREDGE v1 (train/dev/test splits, hard decoys, new retrievers) — added 2026-10-31
Design: DESIGN.md "DREDGE v1". Every command below runs from `evident/`. Nothing here overwrites a pilot file; v1 data lives in
`data/v1/`, reports in `reports/v1/`. Steps 0-4 need no LLM. Long steps print progress; re-running a finished step is refused unless `--force`.

## 0. One-time downloads (outside Claude Code: the sandbox blocks these hosts)
```bash
huggingface-cli download BAAI/bge-small-en-v1.5 --local-dir models/bge-small-en-v1.5      # dense retrieval + hard decoys (~130 MB)
pip install tiktoken
TIKTOKEN_CACHE_DIR=models/tiktoken python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"   # token counts (GPT-4o tokenizer)
```
Without tiktoken, token counts fall back to chars/4 and every prediction records `tokenizer: chars/4`.

## 1. Inventory (done 2026-10-31, ~20 s; re-run only if the source data changes)
```bash
python build/10_inventory_v1.py          # -> data/v1/inventory.jsonl (11,996 questions; 353 invalid BIRD-train gold SQL excluded later)
```

## 2. Splits and question selection (executes ~1,450 test/dev gold queries and every train query; train is the slow part)
```bash
python build/11_split_v1.py --only test dev --workers 8      # minutes
python build/11_split_v1.py --only train --workers 8         # ~4.5k queries on large BIRD-train databases; can take an hour
```
Check `reports/v1/split_summary.json`: `dbs_in_two_splits` must be [] and `pilot_in_test` must be 200.

## 3. Evidence for every candidate, then finalise (no LLM)
```bash
for s in test dev train; do
  python build/03_draft_evidence.py --input data/v1/${s}_candidates.jsonl --out data/v1/${s}_evidence_draft.jsonl --summary reports/v1/${s}_evidence_draft_summary.json
  python build/04_materialise_rows.py --input data/v1/${s}_evidence_draft.jsonl --out data/v1/${s}_evidence_rows.jsonl --summary reports/v1/${s}_evidence_rows_summary.json --max-replay-rows 500000
done
python build/12_finalise_v1.py           # -> data/v1/{test,dev,train}.jsonl + filtered evidence; reports/v1/finalise_summary.json
```
`finalise_summary.json`: test must keep exactly 1,000 (330/330/340 by origin); a WARNING means an origin ran out of valid candidates
(raise `oversample` in configs/v1.json and re-run steps 2-3 for test only).

## 4. Decoy variants (no LLM; hard tier needs the embedding model)
```bash
python build/13_decoys_v1.py --split test --tier random --workers 4
python build/13_decoys_v1.py --split test --tier hard   --workers 4
python build/13_decoys_v1.py --split dev  --tier random --workers 4      # optional: tuning split
```
Verification materialises every level up to +250 for every question and +500/+1000 for a 10% sample (`--verify-sample`, `--verify-max-level`).
Check `reports/v1/decoys_test_{random,hard}.json`: every `*_FAIL` count must be absent, `deterministic` true, and
`first100_lexical_sim_required_mean` higher for hard than for random. Then spot-check hard-tier decoys by hand: 50 variants, does any decoy
table plausibly answer the question? (the answer guard removes the obvious cases only).

## 5. Free retrieval comparison (no LLM): which retriever loses the required tables, and when
```bash
DREDGE_SPLIT=test DREDGE_TIER=random python eval/retrieval_only.py --levels 0 10 50 100 250 500 1000 --out reports/v1/retrieval_test_random.json
DREDGE_SPLIT=test DREDGE_TIER=hard   python eval/retrieval_only.py --levels 0 10 50 100 250 500 1000 --out reports/v1/retrieval_test_hard.json
```
Send me both JSON files before the LLM runs: they decide which retrievers are worth the tokens.

## 6. LLM runs (cost: check with --dry-run first)
Every system and option of Part 1-2 works unchanged; the environment variables choose the dataset. Example for one model:
```bash
export DREDGE_SPLIT=test DREDGE_TIER=random
M=gemini-2.5-flash; L="0 10 50 100 250 500 1000"
python baselines/run_baseline.py --system fullschema_t2s --model $M --levels $L --dry-run          # prompt sizes + token estimate
for S in oracle_schema_t2s bm25_t2s dense_t2s hybrid_t2s graph_t2s fullschema_t2s; do
  python baselines/run_baseline.py --system $S --model $M --levels $L --workers 4 --out data/v1/predictions/$S.$M.test_random.jsonl
done
python baselines/run_baseline.py --system retrieve_read --model $M --levels $L --workers 4 --out data/v1/predictions/retrieve_read.$M.test_random.jsonl
python baselines/run_baseline.py --system retrieve_read --model $M --levels 0 100 --table-retriever hybrid --row-retriever hybrid --rows-per-table 100 \
  --workers 4 --out data/v1/predictions/retrieve_read_hybrid_300.$M.test_random.jsonl
python baselines/run_baseline.py --system question_blind --levels $L --out data/v1/predictions/question_blind.test_random.jsonl
python eval/run_eval.py --predictions data/v1/predictions/*.test_random.jsonl --out reports/v1/eval_test_random --workers 6
```
For the hard tier repeat with `DREDGE_TIER=hard` and `test_hard` in the file names (oracle_schema_t2s does not depend on decoys; run it once).
Budget guide: full schema at +1000 is ~107K tokens per prompt (~107M input tokens for 1,000 questions at that level alone); start with
levels `0 100 500` for full schema if tokens are limited. GPT-4o's 128K context will truncate trailing tables at +1000 (`truncated_tables` records it).
Levels a question's pool cannot fill are skipped by the runner and the scorer (`levels_available` in the manifest), never scored as failures.
