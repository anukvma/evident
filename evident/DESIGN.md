# EVIDENT — design and build log

Name (2026-10-31): **EVIDENT** = Execution-Verified Identification and Diagnosis of Evidence in Noisy Tables (`\benchmark` in the
paper). Replaces REXA-DB, DREDGE and DERIVE (DERIVE clashes with an existing text-to-SQL / table-QA framework). Closest known risk:
the Evidently AI evaluation library (different spelling); fallback EVIDENT-DB. Older notes below use the earlier names.

Name: **DREDGE** = Decoy-scaled Relational Evidence and Diagnosis of Granular Errors (`\benchmark` in the paper; adopted 2026-10-30,
replacing the working name REXA-DB). Title: "DREDGE: Diagnosing Relational Evidence Discovery and Sufficiency as the Search Space Grows".
Rejected for collisions in text-to-SQL/QA: SIFT (SIFT-32B on the BIRD leaderboard), SIEVE (structured-query agent benchmark), SCOUT,
Triage (TriageSQL), EviTab, RAVEL, TRACE-QA. Known out-of-field uses of DREDGE: DREDge (electrophysiology motion correction) and
MuMIn::dredge (R); judged no confusion risk. Kept separate from STRIDE on purpose: STRIDE is an evaluated system, not the benchmark.

## Hypothesis (to be tested by the pilot, not assumed)
Existing resources do not separate (i) candidate-table scale, (ii) relational complexity and
(iii) evidence completeness. Audit result so far (paper Table 1): BIRD, Spider, TQA-Bench and
T-RAG/MultiTableQA each fix the database or corpus; none releases row/cell evidence labels.
TQA-Bench scales rows/context over 2–6 tables per DB; MultiTableQA is web tables without SQL/keys.

## Components
| | Source | Status |
|---|---|---|
| A natural QA | BIRD dev + Spider dev (gold SQL, SQLite) | inventory + pilot selection done |
| B controlled scale | decoy tables / row scaling over A | **design below, not built** |
| C gold evidence | annotated subset of A | **schema below, not built** |

## Decisions made
- **Sources for the pilot:** BIRD dev (1,534 q / 11 DBs) and Spider dev (1,034 q / 20 DBs). All gold SQL
  executes. BIRD train and Spider train are held back (possible later extension).
- **Candidate evidence from SQLite's authorizer** (`evident/sqlinfo.py`): exact real tables/columns a
  gold query reads. This is a *candidate* set only. It is not gold evidence until reviewed, because
  equivalent queries read different columns/rows.
- **Operation label** = most complex of nested > group > aggregate > join > filter > lookup (regex on gold
  SQL; approximate, reported as such). Table-count bucket = distinct real tables read, capped at 4.
- **Splits are by database** (`configs/pilot.json`, seeded). All scale variants of one question stay in
  its split. Split is by DB, so per-source question counts are uneven (e.g. Spider 71/29).
- **Empty-result gold queries are excluded** from the pilot (ambiguous answers).
- **Names are canonicalised to the schema's spelling** (`sqlinfo.read_set`). SQLite identifiers are
  case-insensitive, so Spider queries otherwise report one table under several spellings and inflate
  `n_tables` (found when drafting evidence; steps 1-2 were rerun after the fix).
- **Join keys come from two sources, both recorded:** declared foreign keys among tables read, and
  equality conditions parsed from the gold SQL (aliases resolved). Each key carries `declared_fk` and
  `in_sql`. Many BIRD joins use undeclared keys (e.g. cards.uuid = foreign_data.uuid), so declared FKs
  alone would under-report join evidence. This matters for the join-key-recall metric.
- **Pilot size:** 100 BIRD + 100 Spider. A feasibility target, not a power claim.

## Evidence record (Component C schema, v0 draft)
```
qid, source, db_id, db_snapshot_sha, question, knowledge (BIRD evidence text),
gold_sql, gold_answer, answer_sha1,
evidence: {
  required:   {tables:[..], columns:{table:[..]}, join_keys:[[t1.c1,t2.c2],..],
               rows: {mode: "predicate"|"ids"|"complete_set", spec: ...}},
  supporting: {...}, distracting: {...}
},
operation, n_tables, n_joins, completeness_note, annotator_ids, adjudication
```
Aggregation/count/ranking questions use `rows.mode = "complete_set"`: the *full qualifying set*, or a
formally valid representation from which the answer is derivable. Never just rows one SQL touched.
Plan: auto-draft table/column/join evidence from the read-set, humans review; row evidence from the
reference-query predicates; double-annotate a fraction; report agreement.


## Evidence policy (joins, inner queries, set ops) — implemented in `evident/sqlast.py` + `build/03`
Gold SQL is analysed with a sqlglot AST (all 2,568 BIRD/Spider dev queries parse; the AST's table set
agrees with SQLite's own authorizer on all of them). Names are resolved to the schema's spelling, through
aliases, unqualified columns, correlated references, derived tables and CTEs.

| Case | Policy |
|---|---|
| Join keys | **Required = pairs the gold SQL actually equates.** Each carries `declared_fk`, `derived` (reached through a derived table/CTE) and `suspect`. Declared FKs the query does not use go to `available_fks` and never count in join-key recall (otherwise e.g. `Match` with 22 player FKs inflates the denominator). |
| Undeclared keys | Common in BIRD (e.g. `cards.setCode = sets.code`). Kept as required, noted `undeclared_join_key_used`. |
| Subquery link | `col =/IN/NOT IN (SELECT col2 ...)` becomes an `implicit_join` (kind scalar/in/not_in) between the outer and inner column; scalar-aggregate subqueries (`= (SELECT MAX(x) ...)`) resolve to the column inside the aggregate. Correlated predicates (`EXISTS ... T1.id = T2.hero_id`) become ordinary join keys. |
| Steps | Every (sub)query is a `step` with its own reads and parent. A system is credited for an inner result only if it retrieved that step's evidence. |
| Set operations | Recorded in `set_relations` (UNION/INTERSECT/EXCEPT). Connectivity is not required for set-op queries. |
| Row mode | `complete_set` if aggregate, group, ORDER BY+LIMIT (ranking), negation (NOT IN/EXISTS, EXCEPT), set op, or an IN/NOT IN/ranking/aggregate inner query; else `predicate`. Negation and ranking need the *whole* qualifying set: one missing row changes the answer. |
| Gold-SQL suspects | Undeclared join between two tables' *sole* primary keys (e.g. `Match.id = Player.id`) is `gold_join_suspect` -> human review (keep/fix/drop question). |
| Review queue | `needs_review` holds real anomalies only (unconnected tables, suspect join, unresolved link, AST/SQLite disagreement). Informational `notes` do not enter the queue. |

Lessons from building it: SQLite is case-insensitive for identifiers (Spider duplicates); Spider uses
double-quoted *string literals* that the SQLite dialect parses as identifiers (never treat an unresolved
side of `=` as a join); BIRD gold SQL can be semantically dubious.

## Row evidence (step 04) — what it is and is not
Each step's `evidence_sql` returns the `rowid` of every base table the step reads, keeping FROM/JOIN/
WHERE and dropping GROUP BY/HAVING/ORDER BY/LIMIT/aggregation. The union over steps is the record's row
evidence. Row sets > 5,000 rows keep counts + hash + the defining SQL (the SQL is the spec).
* **Negation** (`NOT IN`, `NOT EXISTS`): negated conjuncts are dropped from the outer step, so evidence =
  all candidates passing the other filters + the complete inner set. Keeping only survivors would hand a
  system the answer without the rows needed to know it is the answer. (Found by the necessity probe.)
* **Correlated subqueries** pull in the referenced outer tables (+ their subquery-free outer filters),
  giving a superset (`correlated_superset`).
* **Sufficiency check (all 200 pass):** a database containing ONLY the evidence rows returns the gold
  answer. **Necessity probe:** emptying a required table changes the answer for 199/200; the exception
  (`spider-dev-927`) is a *vacuous* EXCEPT (no overlap in the data). Sufficient, not proven minimal.
* **Numbers:** median ~23 evidence rows; heavy tail (max 383,293) from aggregates over whole tables.
  Row-level recall on such questions is dominated by complete-set requirements -- exactly the cases a
  top-k retriever cannot satisfy; report them separately, not averaged.
* Pitfalls hit: Spider's double-quoted *string literals* are case-folded by sqlglot's `qualify` unless
  made explicit (`Analyzer.normalise_quotes`); `sqlite_sequence` must not be recreated; some Spider rows
  are invalid UTF-8; DETACH needs a commit.

## Component B (scale variants) — decoy tables, implemented (`evident/decoys.py`, `build/05`)
**Definition.** Level N = the question's own database (untouched, attached read-only) + N real tables copied
from *other* BIRD/Spider databases. Levels 10/50/100 are **nested** (10 inside 50 inside 100) so a change in
accuracy is attributable to added search space only. Stored as manifests (source, db, table, name in variant),
not as data; rows = first `row_cap` (20,000) rows by rowid; `materialise()` rebuilds a variant deterministically
(manifest hash is stable across rebuilds).

**Pool and leakage.** Spider-*train* databases (146 DBs, none used by any dev question) + pilot DBs of the
*same split*. Never the question's own DB, any DB with the same id (`formula_1` exists in both sources), the
other split's DBs, or Spider-dev DBs (a later, larger benchmark would draw questions from them). BIRD questions
get same-source decoys interleaved with Spider ones (BIRD has no train DBs here, so only same-split BIRD DBs).

**Answer-ambiguity controls.** Exclude decoys whose table name equals any own-DB table or whose column-name
Jaccard with any own-DB table is >= 0.5 (a look-alike could be a second valid source and make "irrelevant"
false). Measured max Jaccard decoy-vs-required table: 0.40 (median 0).

**Giveaway controls.** (1) Decoy DDL is regenerated from PRAGMAs; foreign keys are kept only when their target
is in the variant, so "has a valid FK" does not reveal real tables (sqlglot DDL rewriting was too brittle:
29/948 statements failed). (2) **Size matching:** decoys follow the row-count distribution (log10 bins) of the
question's own DB, ordered so every nested prefix tracks it. Before matching, BIRD questions had required
tables with median 4,500 rows vs decoys with median 15 -- size alone would have identified real tables.
After: total-variation distance 0.09 (BIRD) / 0.02 (Spider); ~90% of the size quota is met, the rest is filled
by relaxation. (3) Name collisions (decoy vs decoy only) get a numeric suffix, not a prefix.

**Verified for all 200 x 3 variants:** gold answer unchanged; table names unique; every decoy FK resolves;
candidate-table count = own tables + N; deterministic manifests; no leakage. *Caveat:* answer-preservation and
namespace checks are close to true by construction (decoys live under new names); the controls that matter are
the look-alike and size-matching ones above.

**Known residual threats (state in the paper):**
* Required tables are larger than the average table of their DB, and we match the *whole-DB* distribution, so
  "bigger tables are more likely to be real" survives weakly (BIRD: required median 4,500 vs decoy median 412).
* Decoy rows are capped at 20,000; BIRD's largest tables have ~1M rows.
* Spider-style decoys are still the majority for BIRD questions (about 70% of BIRD decoys); style
  (naming, column vocabulary) may remain a giveaway. Test with a "decoy-vs-real" classifier probe before trusting
  the scale curves.
* Row-scale variants (second axis of Component B) are **not built yet**.

## Evaluation harness (`evident/evaluation/`, `eval/run_eval.py`)
System-agnostic: a system writes predictions (format in `evaluation/schema.py`); the harness scores them.
Any SQL a system returns is **executed by the harness** on the right variant (level 0 = original DB, level N =
N decoys + original), read-only, 60 s timeout, SELECT/WITH only. All fields except system/qid/level are optional.
**An unavailable metric is NaN, never 0**; a missing (qid, level) line is a wrong empty answer.

| Metric | Definition |
|---|---|
| exec_accuracy | executed SQL (or supplied rows) equals gold as a **multiset** of rows; numbers compare to 6 significant digits (5 == 5.0), NULL kept, order ignored (documented deviation from Spider's ordered check) |
| table_recall@k, full_coverage@k | k in 1,3,5,10,20,50 over `ranked_tables`; full_coverage = all required tables in top-k. Recall@k is capped at min(k,|required|)/|required| |
| *_selected | recall/precision of the final `selected_tables` |
| column_recall/precision | over required `table.column`; "*" expands to all columns |
| join_key_recall/precision | unordered pairs; gold = joins the SQL equates + subquery links; NaN when the question has no join |
| row_recall/precision, row_complete_set | micro over (table,rowid); gold = the materialised evidence (step 04); "ALL" counts the whole table |
| **evidence_sufficient** | the gold query still returns the gold answer on a DB built from ONLY the system's retrieved rows |
| cost_* | latency, preprocessing, tokens, LLM calls, peak memory, as reported by the system |

Failure attribution (RQ4), first failing stage, only for stages the system exposes: no_answer, missed_table,
missed_column, missed_join_key, incomplete_evidence, sql_failure, wrong_answer_despite_evidence
(wrong_answer when no retrieval info exists, i.e. unattributable). "stale index" needs the update track (not built).
Aggregation: by level/source/operation/n_tables/rows_mode; **scale degradation** Delta M(level) = M(level) - M(0)
paired by question; **CIs are database-cluster bootstrap** (questions of one DB are correlated); paired
system-vs-system differences with the same bootstrap. No composite score.

**Self-test** (`python tests/test_harness.py`): 8 trivial systems (`baselines/trivial.py`) with exactly known
scores -- oracle all-perfect, noop NaN/no_answer, random table ranking (recall@10 0.94 -> 0.10 from 0 to 100
decoys), all-rows (recall 1, precision 0.21), and four corrupted systems that must be attributed to
missed_table / sql_failure / wrong_answer_despite_evidence / incomplete_evidence. 30 checks pass. The first run
caught two bugs in the TESTS, not the harness (recall@1 is mathematically capped for multi-table questions; a
generator "removed" a required table by picking the table it was supposed to remove).

Known limits: evidence_sufficient and row metrics need row ids, so text-only systems get NaN there; the
"incomplete_evidence" stage trusts the gold row sets, which are sufficient but not proven minimal; metric
definitions are not yet validated against human judgement.

## Baselines implemented (`baselines/`, run via `RUNBOOK.md`)
| System | What it is | LLM? |
|---|---|---|
| `question_blind` | GBM on schema features ONLY, trained to predict required tables, out-of-fold by database | no |
| `oracle_schema_t2s` | LLM writes SQL given exactly the gold tables (ceiling; isolates reasoning from retrieval) | yes |
| `bm25_t2s` | BM25 over table docs (name, columns, sample values) -> top-5 -> LLM writes SQL | yes |
| `fullschema_t2s` | every candidate table's schema in the prompt (full-context analogue, 300k-char cap) -> LLM writes SQL | yes |
| `retrieve_read` | **no SQL.** BM25 picks 3 tables, SQLite FTS5 BM25 picks 30 rows per table (small tables read in full), LLM answers from those rows as a flat JSON list | yes |
Not yet: dense/hybrid retrieval, graph-aware retrieval, T-RAG/TQA-Bench reproductions.

Design points: (1) **LLM access reuses STRIDE's `LLMClient`** (`evident/llm.py` loads it by path; no copied
credentials/endpoints), with an on-disk response cache for exact reruns, token estimates (chars/4), and `mock:*`
models for offline tests. (2) **Uniform DDL:** every table in a prompt (own and decoy) is serialised by the same
PRAGMA-based generator, so the LLM cannot see which tables are real from DDL style. (3) **SQL-derived evidence:**
columns/joins/rows for text-to-SQL systems are those their own SQL uses (our evidence rewrite applied to the
predicted SQL), flagged `evidence_source = sql_derived`; failures there are labelled `sql_missed_*` /
`sql_incomplete_evidence` (SQL planning), distinct from retrieval's `missed_table`. (4) Baseline tests with
mocks (`tests/test_baselines.py`, 15 checks) show the mock gold-SQL LLM reproduces gold evidence exactly (row and
column recall 1.0, sufficient) at levels 0 and 100. (5) `question_blind` table_recall@5 on the full pilot:
0.82 at level 0 (tiny DBs) and **0.02 at level 100 (chance)**: the decoy design does not hand out the answer.
Measured prompt cost for the full pilot is in RUNBOOK.md (fullschema ~4.5 M input tokens, ~12 k per prompt at 100 decoys).
No real-LLM result exists yet; nothing here may be cited as a result.

### Non-SQL path and the value metric (added with `retrieve_read`)
* `exec_accuracy` (strict row multiset) cannot judge a text answer, because the model cannot know the gold column layout.
  `value_accuracy` compares the FLAT multiset of normalised cell values (strings case-folded and stripped, '1,234' == 1234,
  6 significant digits, layout/order ignored). It is defined for every system, so SQL and non-SQL systems share one scale.
  It is looser than exec_accuracy and must not be mixed with it. A prediction with only `answer_values` has exec_accuracy = NaN;
  failure attribution then uses value_accuracy.
* `retrieve_read` evidence = rows placed in the prompt (`evidence_source = context_rows`), so row recall and evidence
  sufficiency measure what the model actually saw. With a mock LLM that returns the gold values, value_accuracy is 1.0 and
  evidence_sufficient is 0.00 on the first 15 questions: top-k rows cannot cover complete-set questions, and lexical row
  search misses coded values (question says "female", the cell holds 'F') and cannot tell which column a number belongs to
  (an "ID of 30" matches thousands of rows). These are properties of the method family, not bugs; a dense or hybrid
  retriever should be added before drawing conclusions about retrieve-and-read in general.
* Index build time per table is in `data/cache/fts/manifest.json` (preprocessing cost; 184k-row table: ~6 s).
* Bugs found while building it: concurrent index builds raced on a temp file ("disk I/O error"); fixed with a lock.

## Pilot results v1 (gemini-2.5-flash, 200 questions x 4 levels, one run) — reports/eval_final, paper Section 7
Regenerate paper numbers: `python eval/make_paper_tables.py --outdir ../LaTeX2e+Proceedings+Template+ZIP` (writes results_macros.tex +
tab_*.tex; the paper has NO hand-typed result numbers). Side analyses (set-vs-multiset, noise floor, manual review): reports/side_analyses.json.
* Oracle schema value_acc 0.743 (BIRD 0.625, Spider 0.860); full schema 0.716; BM25 top-5 0.539. Question-blind: table R@10 0.995 -> 0.059.
* **Scale hurts retrieval pipelines, not full-schema prompting (at <= ~110 tables / ~12K tokens):** BM25 value_acc -26.5 points
  (95% CI -33.7 to -19.5) from +0 to +100 tables; full schema -1.0 (CI -4.1 to +2.7); oracle +0.5. The central hypothesis
  "decoys hurt a strong LLM" is therefore NOT supported for full-context prompting at this scale; it is supported for BM25 retrieval.
  Full-schema cost: input tokens 924 -> 12,427 (about 16x BM25). Untested: >110 tables.
* Noise floor: oracle prompt is identical at every level, yet 7/200 questions flip correctness across levels (LLM sampling).
* BM25 failures: 30.4% missed_table (93/93 verified by hand at L100 that the table was absent). Oracle errors occur with the right tables shown.
* Accuracy falls steeply with tables read (oracle 0.863 -> 0.365 from 1 to 4+ tables; 4+ group has 26 questions).
### Measurement problems found while checking the results (all disclosed in the paper)
1. **value-accuracy scale delta was mislabelled** in an early headline (it showed the exec-accuracy delta, -25.5 instead of -26.5). Fixed in aggregate.py/consolidate.py.
2. **Multiset vs set:** our execution accuracy (multiset) is stricter than BIRD's set equality: +3.0 to +4.0 points at level 0. Absolute numbers are NOT comparable with published BIRD/Spider accuracy.
3. **Evidence sufficiency is conservative for equivalent routes:** of 7 distinct oracle questions with correct answer but "insufficient" evidence,
   6 skip a bridge/dimension table via a valid shortcut and 1 is a coincidence (spider-dev-912). So "right answer, wrong evidence" is an UPPER bound.
   A better definition would accept alternative evidence sets (equivalence-aware sufficiency); not built.
4. question_blind shows value/exec accuracy 0.000 in headline tables because the harness counts "no answer" as wrong; the paper prints "--".
5. SQL-derived evidence means columns/joins/rows reflect the query's own use of tables, so `sql_*` failure labels are planning errors, not retrieval.

## Single-table comparison datasets (WTQ, HiTab, AIT-QA) — related work / Table 1 only
Added to the paper as a "Single-table TableQA" related-work subsection and three rows of the coverage table, from their RELEASED DATA
(counts measured locally): WTQ 14,152 train + 4,344 test questions over 2,108 tables, answers only; HiTab 10,672 questions
(7,417/1,671/1,584), every training question has linked cells + answer formula + aggregation label (README: 3,597 tables, ACL 2022);
AIT-QA 515 questions over 113 tables in our clean copy, answers only, hierarchy flag, 370 table-driven / 145 KPI-driven.
Consequence for the novelty wording: **HiTab does release cell-level evidence** (single table), so the claim is limited to evidence for
RELATIONAL questions (several tables, join keys, complete qualifying set, verified by execution). They are NOT used experimentally: the
table is given with the question, so the candidate-table-scale axis does not apply. Bibliography: arXiv ids from TQA-Bench's reference list;
AIT-QA published venue and WTQ/HiTab pagination still to verify. Paper length: 13 pages with TODO notes, 12 without.

## Evaluation gaps closed in tooling (2026-10-24) — RUNBOOK.md Part 2 has the commands
| Gap | Fix | Status |
|---|---|---|
| Full-schema flat at 110 tables; breaking point unknown | decoy levels **250 and 500**, APPENDED (build/05b): levels <= 100 byte-identical (asserted; 0/75 rebuilt prompts differ from the stored Gemini run); 1,600 integrity checks pass | data ready, **LLM runs pending (you)** |
| Question-blind control printed accuracy 0.000 | retrieval-only predictions get NaN answer metrics + failure `retrieval_only` | done, tested |
| No comparable-to-BIRD accuracy | `exec_accuracy_set` (set equality) alongside the stricter multiset metric | done; needs re-scoring |
| Sufficiency flags equivalent routes | `evidence_sufficient_eq`: strict OR a **human-confirmed** alternative reference query (data/alt_reference_sql.json; build/07 proposes, a person confirms per alternative; `eval/rescore_sufficiency.py` updates old runs in ~1 min) | mechanism done; **33 proposals / 13 questions await human adjudication** |
| One run per system | `--run-id` + `eval/variance.py` | done; **repeat runs pending (you)** |
| Scoring ~40 min | `eval/run_eval.py --workers N`; parallel output verified identical to sequential (0 differing cells) | done |
| Evidence labels never human-reviewed | build/08 sheets (40 stratified questions, 2 annotators) + `eval/annotator_agreement.py` (kappa checked against a hand calculation) | **annotation pending (people)** |
Safeguards added after near-misses: build/05 refuses to overwrite manifests (regenerating would silently change which decoys sit at each
level); runner defaults stay at levels 0/10/50/100 (250/500 are expensive); the scorer infers levels from the prediction files so absent
levels are never scored as failures.
Findings from the work: (1) full-schema prompts: ~28.6k tokens at 250 decoys, ~54k at 500, ~16.2M for the two levels; (2) at 500 decoys
each pool table appears in ~122 of 200 variants (877 pool tables in total), so level 500 is close to "the whole Spider-train pool" for everyone;
(3) the first adjudication design (confirm by question) would have accepted non-equivalent variants (a bird-113 variant adds type='OWNER'),
so adjudication is per alternative; (4) the 33 proposals include near-duplicate rewrites (bird-1526 has 9).
Still open: contamination probe, `retrieve_read` and second-model runs, dense/hybrid retrieval, row-scale and update tracks, name collision search.

## Pilot results v2 — six levels, five systems, human adjudication (2026-10-28) — reports/eval_final_v5, paper Section 7
Consolidated from: re-scored Gemini runs (steps 1), adjudication of alternative references (step 2), levels 250/500 (step 3), retrieve_read (step 4).
Steps 5 (second model), 6 (variance) and 7 (annotation sheets) are NOT done. Paper tables/numbers: `python eval/make_paper_tables.py --final reports/eval_final_v5`.
**Findings**
* Full-schema prompting does NOT break up to +500 tables (~53K tokens): value_acc 0.715 -> 0.730, delta +1.5 (CI -1.2..+4.7), +250: -3.0 (CI -6.9..+1.6).
  The "decoys hurt a strong LLM" hypothesis holds only for retrieval pipelines: BM25 0.680 -> 0.400 (-28.0, CI -35.8..-19.7), flattening after ~100.
  Full-schema cost is tokens (924 -> 53,190, ~75x BM25's 712). Open: larger collections / smaller-context models.
* Retrieve-and-read (no SQL; 3 tables x 30 rows, keyword search): value_acc 0.206 (0.310 at +0). Accuracy is 0.750 when the rows read were sufficient
  (80 q) and 0.017 otherwise (120 q): evidence sufficiency explains it. For the 69 questions needing >90 rows: 2.9% vs 47.8% for BM25 text-to-SQL.
  My earlier hypothesis that it fails on *complete-set* questions was WRONG as stated (0.302 predicate vs 0.313 complete_set): it fails where the
  evidence exceeds its read budget AND on small sets (lexical row search misses coded values, cannot tell columns). Intro sentence rewritten accordingly.
* Adjudication (1 reviewer, 33 alternatives: 25 confirmed, 8 rejected; agreed with my suggestions on 32/33, but the reviewer saw my suggestions, so
  this is NOT independent): correct-but-insufficient shares fall oracle 4.8% -> 0.7%, full 3.3% -> 1.0%, BM25 7.4% -> 3.8%. Disputed bird-131:0 (reviewer
  confirmed, assistant would reject: different measure/relationship; ranking identical on this data) affects exactly one prediction.
* Set-equality accuracy is 2.8-4.5 points higher than multiset accuracy; order and scale effect unchanged (BM25 -28.5).
**Bugs and data problems found and fixed (all disclosed or harmless to conclusions)**
1. Prediction file hygiene: resumed runs append retry lines; 90 of 91 LLM errors in the bm25 L250/500 file were 401 (token expiry) later recovered; the scorer
   keeps the LAST line per (system, qid, level). One ReadTimeout remained unrecovered (bm25, spider-dev-535, L250).
2. My retrieval-only rule (added for the question-blind control) swallowed (a) that failed call and (b) a genuine refusal (bird-95, L500), silently dropping both
   from accuracy. Fixed: llm_error -> `system_error` (excluded, reported, warned); abstained -> counted wrong (`no_answer`). Tools: eval/rescore_rows.py.
3. Latency is unusable across levels (oracle prompt identical, median 3.0 -> 42.3 s between runs = gateway load). Paper reports tokens only.
4. The question-blind control was retrained over six levels; its numbers changed slightly (L100 recall 0.059 -> 0.047). Paper uses the new ones.
**To finish the pilot (you):** re-run the one failed call, then rescore only that row:
  python baselines/run_baseline.py --system bm25_t2s --model gemini-2.5-flash --levels 250 500 --workers 4 --out data/predictions/bm25_t2s.gemini-2.5-flash.L250_500.jsonl   # resumes, retries only failed calls
  python eval/rescore_rows.py --per-question reports/eval_final_v5/per_question.jsonl --predictions data/predictions/*.gemini-2.5-flash.jsonl data/predictions/*.gemini-2.5-flash.L250_500.jsonl --status system_error --out reports/eval_parts6_eq/main_fixed2
  python eval/consolidate.py --parts reports/eval_parts6_eq/main_fixed2 --out reports/eval_final_v6 && python eval/make_paper_tables.py --final reports/eval_final_v6 --outdir ../LaTeX2e+Proceedings+Template+ZIP
  then delete the "[TODO: Re-run that call ...]" sentence in Section 7.1 of the paper.

## Second model: GPT-4o (SQL systems, levels 0-100) — reports/eval_final_gpt4o, reports/model_comparison.json, paper Section 7.4
Predictions complete (800 per system; 1 early AttributeError recovered by retry; 23 BM25 refusals counted as wrong). Prompts are byte-identical to Gemini's (2,400/2,400),
so differences are the model. Commands: RUNBOOK Part 2 step 5; comparison: `python eval/compare_models.py --a reports/eval_final_v6 --b reports/eval_final_gpt4o`.
* The pattern REPLICATES: BM25 -25.0 points at +100 (CI -32.7..-17.6), full schema -1.5 (CI -5.9..+3.3, n.s.), oracle +0.5 (flat); same system ranking.
* GPT-4o is lower with perfect retrieval: oracle 0.695 vs 0.743 (paired -4.8, CI -9.3..-1.0, significant); full schema -3.5 and BM25 -0.6 (not significant).
* GPT-4o refuses to write a query in 2.9% of BM25 predictions (tables shown cannot answer); Gemini 0.0% at these levels.
* Equivalence-aware sufficiency uses alternatives adjudicated from GEMINI's answers. Exposure for GPT-4o is small: correct-but-still-insufficient is 0.7% / 1.5% / 1.7% of
  its correct answers (oracle/full/BM25), only 0/2/3 questions have no reviewed alternative. Not adjudicated separately; stated as a limitation.
* NOT done for GPT-4o: levels 250/500, retrieve-and-read, question-blind, variance. The claim "a second LLM reproduces this" is therefore limited to the SQL systems up to +100.
**Paper length work (clean = no TODO notes = what gets submitted: 12 pages; draft with notes: 13).** To fit the new section I (a) replaced the planned-dimensions table with one
sentence, (b) rewrote Metrics, Baseline Systems and Evidence Annotation Protocol to describe what was actually done (they still contained drafting directives promising
latency, memory and a sample inspection that the pilot does not have), (c) shortened long author lists to "et al." (LNCS style), (d) removed sentences duplicated by tables.

## DERIVE v1 (written as DREDGE v1 in older notes) — train/dev/test splits, hard decoys, new retrievers (2026-10-31; RUNBOOK Part 3 has the commands)
**Splits (option A, strict separation; decided with the author).** By database. test = all 11 BIRD-dev + 20 Spider-dev databases +
34 BIRD-train databases; dev = 8 and train = 27 BIRD-train databases (dealt along table-count order, configs/v1.json). Test holds
1,000 questions (330 Spider dev / 330 BIRD dev / 340 BIRD train) INCLUDING the 200 pilot questions, whose records are kept as they are.
**Spider-train databases are never a question source**: they are the test-only decoy pool. Reason: at +500 the pilot already used almost
the whole Spider-train pool, and a system trained on a split that contains test decoys could learn "this table is never relevant".
Train/dev questions draw decoys only from their own split's databases, so their maximum level is lower (train has 197 tables).
**Selection.** Same stratified round-robin as the pilot (evident/selection.py). Test/dev candidates are oversampled by 15%; evidence is built
for all (build/03-04, unchanged policy); build/12 keeps the first quota of valid candidates in pick order. Dropped: invalid gold SQL
(353 BIRD-train queries reference missing columns or do not parse), AST/SQLite disagreements (3), empty answers, gold timeouts (60 s),
evidence that does not reproduce the answer, and evidence above 500,000 rows (`too_large`: the sufficiency replay copies the evidence into
memory; the pilot maximum is 383,293; counted before loading). Tables without a rowid (3, all in BIRD-train `language_corpus`) cannot carry
row evidence; questions reading them drop out, and they are never decoys.
**Decoys (build/13, configs/decoys_v1.json).** Levels 10/50/100/250/500/1000, nested within a tier; a question gets the levels its pool can
fill (`levels_available`); runner and scorer skip the rest. Pilot rules kept (empty, same name, column-name Jaccard >= 0.5, size matching,
20,000-row cap, FK-only-if-present DDL). New in v1:
* *Answer guard* (both tiers): reject a decoy that contains one of the gold query's compared string literals in a column whose name matches
  the compared column (token Jaccard >= 0.5). Blocks the obvious second sources; a human spot check of hard-tier variants is still needed.
* *Hard tier*: candidate tables ordered by reciprocal-rank fusion (k = 60) of lexical schema similarity to the required tables (char 3-5-gram
  TF-IDF over table + column names) and semantic question-to-schema similarity (bge-small). Both tiers record `lexical_sim_required` per decoy
  so the tiers can be compared. Smoke test (12 questions, stub embeddings): first-100 mean 0.091 hard vs 0.052 random.
* *Style mix*: BIRD-train test databases are now same-split decoys, so BIRD questions get mostly BIRD-style decoys (smoke: ~91% of the first
  100 vs ~30% in the pilot), which targets the style cue the shortcut probe found (BIRD AUC 0.62). Re-run build/06's probe on v1 to confirm.
* The pilot selector was refactored into a shared `_choose`; pilot manifests regenerate byte-identically (sha1 e897891d...).
**New systems and metrics.** `dense_t2s` (bge-small over the same table documents BM25 sees), `hybrid_t2s` (RRF of BM25 and dense),
`graph_t2s` (personalized PageRank over the variant's FK graph, restart at the hybrid top 10 weighted by fused score, alpha 0.5); all show
top-5. `retrieve_read` gains `--table-retriever hybrid` and `--row-retriever dense|hybrid` (dense rows: first 20,000 rows per table,
cached, build time reported as preprocessing); its pilot configuration keeps its name. `exec_accuracy_struct`: column order ignored,
row/column association kept (catches the swapped-values case value accuracy accepts). Token counts: tiktoken o200k_base when available,
recorded per prediction (`tokenizer`). `eval/retrieval_only.py` compares the four retrievers with no LLM.
**Dataset switch.** `DREDGE_SPLIT` (pilot default | test | dev | train) and `DREDGE_TIER` (random default | hard) select files in
`paths.active()`; with neither set every pilot command behaves exactly as before.

## Next steps
1. [ ] Human-review the pilot (`data/pilot.jsonl`): ambiguity, answer sanity, difficulty spread.
2. [x] `build/03_draft_evidence.py` (policy above): v0 evidence records (tables, columns, join keys). 200 drafted; 184 clean,
       13 use an undeclared join key, 4 have tables not connected by any join key -> review first.
       Row evidence is still a placeholder (`spec`): materialise qualifying rows next.
3. [x] `build/04_materialise_rows.py` (200/200 sufficient)
3b. [x] `build/05_make_decoys.py`: nested decoy variants, 600/600 checks pass.
3c. [x] shortcut probe (`build/06_shortcut_probe.py`, `reports/shortcut_probe.json`), chance AUC = 0.5:
       held-out-DB classifiers ~0.51 overall (BIRD 0.62 with style features); strongest single features pooled:
       FK-component size 0.69, column underscore style 0.63, varchar(n) types 0.62, row count 0.62; within one
       question mean AUC 0.55-0.66 (about half of BIRD questions >= 0.7 on one style feature). Size matching
       works (row count ~ chance); naming/type style and FK-component size leak MODERATELY, mostly for BIRD.
       Among a question's own tables, structure features predict "required" with AUC up to 0.69 (BIRD), so
       relevance retrieval is partly easier than it looks. Probe uses hand-made schema features only; an LLM
       reading schema text is untested.
       Decision: accept for the pilot; report it; add a QUESTION-BLIND schema-only baseline as a control for
       every scale curve (if it nears real systems' table recall at 100 decoys, flag the result).
       Options if needed later: normalise decoy type strings to the question DB's style.
3d. [ ] row-scale variants (second axis).
4. [x] `eval/` harness + self-test (see section above).
5. [~] `baselines/`: question-blind, oracle-schema, BM25 and full-schema text-to-SQL built + tested with mocks;
       real-LLM runs pending (RUNBOOK.md). Still to do: dense, hybrid, graph-aware retrieval.
6. [ ] Update paper placeholders from measured numbers only.

## Reproduce
```
python build/01_inventory.py      # -> data/inventory.jsonl
python build/02_select_pilot.py   # -> data/pilot.jsonl, reports/pilot_summary.json
python build/03_draft_evidence.py # -> data/evidence_draft.jsonl
python build/04_materialise_rows.py # -> data/evidence_rows.jsonl (sufficiency + necessity checks)
python build/05_make_decoys.py    # -> data/decoy_manifests.jsonl, reports/decoys_summary.json
python build/06_shortcut_probe.py # -> reports/shortcut_probe.json
python tests/test_harness.py      # harness self-test (30 checks)
python baselines/trivial.py --out data/predictions   # reference systems
python eval/run_eval.py --predictions data/predictions/*.jsonl --out reports/eval_run
```

## Folder rename (2026-10-31)
The code folder and package `reltable/` are now `evident/` (`from evident import ...`), and the paper folder `rexa_acl/` is now
`evident_acl/` (`evident_acl.tex`, `evident.bib`). Older notes above use the old paths. Data, reports and caches moved with the folder;
caches are keyed by content (prompt hash, table name), so nothing needs rebuilding.
