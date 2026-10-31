# Revision plan — response to the PAKDD-style review (received 2026-10-30)

Reviewer verdict: weak reject / borderline, "promising idea, not yet submission-ready". Three dominant concerns: (1) novelty against T-RAG / MMQA /
TQA-Bench, (2) annotation validity, (3) experimental strength. This plan treats those as gates, not as polish.

## A. Triage of every comment
Legend: DONE = already addressed with data; PLAN = a work package below; UNVERIFIED = I cannot confirm it from sources I hold; PUSHBACK = I disagree or qualify.

| # | Review comment | Verdict | Evidence / action |
|---|---|---|---|
| 3 | Sharpen novelty vs T-RAG, MMQA, TQA-Bench, TANQ | PARTLY DONE, WP1 | **Now verified from the four papers (section B).** T-RAG = Findings of ACL 2026 pp. 38133-38152; TANQ = TACL 13:461-480 (2025); MMQA = ICLR 2025. Related Work, Table 1 and the bibliography already carry the verified positioning. Still missing: MT-RAIG (not supplied) and the published TQA-Bench venue (the PDF shows an IEEE notice but no venue name). |
| 4.1 | Gold evidence not validated; reproducing the answer != complete evidence | PARTLY DONE, WP2 | Completeness probe run (below): adding random non-evidence rows never changed the answer in 1,200 trials. Human 2-annotator validation still missing. |
| 4.2 | Sufficiency needs justification; 33 alternatives by 1 reviewer is thin | PLAN WP2 | Valid. Present strict sufficiency as primary, equivalence-aware as an adjudicated secondary; second reviewer for the 33; state limits. |
| 4.3 | Benchmark too small | PLAN WP7 | Valid. Pipeline is automatic for all 2,568 BIRD+Spider dev questions; the cost is LLM tokens and validation, not construction. |
| 4.4 | Jaccard filter removes the hardest, most realistic decoys | PLAN WP5 | Valid. Add schema-similar and semantically similar tiers, with a non-answer guarantee. |
| 4.5 | BM25 is a weak baseline | PLAN WP3 | Valid. Dense, hybrid, graph-aware (FK) retrieval. |
| 5.1/5.2 | Do not generalise beyond ~510 tables / 53K tokens | PLAN WP6 | Valid; the paper already says so, but 1,000-2,000 tables must be tested. GPT-4o (128K context) should reach its limit near 1,000 added tables. |
| 5.3 | Retrieve-and-read comparison is budget-limited | PLAN WP9 | Valid and I flagged the same weakness. Vary budget 30/90/300, add dense/hybrid row retrieval. |
| 5.4 | Only 31 clusters, one run | DONE + WP8 | Per-database analysis done (below): not driven by a few databases. Repeats still needed. |
| 6 | "Perfect table selection ~74%" ambiguous | DONE | Abstract now says "the oracle schema". |
| 6 | 37% needs a level and denominator | DONE | It was pooled over six levels; the real trend is **10% (+0) to 51% (+500)**. Abstract and Section 7 now say so. |
| 6 | Dev/test split described but all 200 reported together | DONE (numbers), WP4 (text) | Effect holds in both (below). Add the split table to the paper. |
| 6 | Value accuracy flattens columns | DONE (audit), WP4 (metric) | Audit below: negligible for SQL systems. Still add a structured metric. |
| 6 | Recall@10 vs what the system saw | DONE (numbers), WP4 (text) | Report recall of the tables actually shown (0.595 at +500, not 0.665). |
| 6 | Tokens are chars/4 | PLAN WP4 | Capture real usage from the API or use a tokenizer. |
| 6 | GPT-4o only to +100 | PLAN WP6 | Already stated; extend. |
| 6 | Failure categories need definitions/denominators | PLAN WP4 | Categories are mutually exclusive (first failing stage); say so. |
| 6 | Release in future tense | PLAN WP10 | Needs an anonymized repo (rename folders, strip internal references, replace the internal LLM client). |
| 7 | "ERE" metric = answerable-with-sufficient-evidence / evidence units | PUSHBACK | Mixes units (rows, tables, tokens) across systems and is dominated by the denominator. Instead plot sufficiency and accuracy against rows/tokens retrieved (a cost frontier), computed from data we already have. Supplementary only. |
| 7 | Candidate tables up to 1,000 | PLAN WP6 | Our decoy pool is ~877 non-empty tables; 500 already reuses ~64% of it. Going further needs more source databases (BIRD train). |
| 9 | Title, acronym, abstract emphasis | PLAN WP10 | Adopt "Benchmarking Evidence Retrieval and Sufficiency in Scalable Relational Question Answering"; expand REXA (the name itself is still unchecked for collisions). |
| 9 | Resolve TODOs, MMQA citation, licenses | PARTLY DONE | T-RAG venue, MMQA and TANQ entries fixed. 7 notes left: BIRD/Spider licenses, human label review, TQA-Bench venue, WTQ/HiTab pagination and DOI, AIT-QA venue. |

## B. Positioning evidence, verified from the four papers (reference_papers/)
Every cell below was read in the paper; "none found" means a search of the paper text found nothing, not that it is proven absent.

| | T-RAG / MultiTableQA (Findings of ACL 2026) | MMQA (ICLR 2025) | TANQ (TACL 13, 2025) | TQA-Bench (arXiv v2) | **REXA-DB** |
|---|---|---|---|---|---|
| Data | 20K web tables (HybridQA, SQA, TabFact, WTQ) split by row/column into 60K; 3 task types; Spider only as a single-table retrieval side test | 5,000 two/three-table samples on Spider DBs; questions LLM-written from 45 SQL templates | Open-domain questions seeded from QAMPARI; answers are tables built from Wikipedia text/tables/infoboxes | 10 relational DBs (BIRD, DataGov, WorldBank), 2-6 tables each, template questions | Natural BIRD + Spider dev questions |
| Relational schema | "lack explicit relational schema" (their words) | Yes: PK/FK annotated by experts | No | Yes (FK joins) | Yes |
| What scales | corpus (60K tables, latency); number of retrieved tables k | corpus given, **no size variation found** | none | serialized context 8K-64K tokens (rows) | **number of irrelevant tables, nested, question/answer fixed** |
| Evidence | source tables (retrieval recall/acc) | tables (P/R/F1), PK/FK labels | **cell-level source attribution** for answer cells | none | tables, columns, join keys, rows; **executable sufficiency** |
| Answer metric | accuracy, EM | EM/partial match judged by GPT-4-turbo; ROUGE/BLEU for SQL (not execution) | F1 over table cells | accuracy | execution (strict/set), structured, value |
| Human validation | N/A (existing human-annotated sources) | 2 experts + a 3rd for disagreements; agreement 86% (2-table), 82% (3-table) | automated pipeline, human evaluation | symbolic generation | **missing (WP2)** |
| Failure attribution | no | no | failure-case discussion | per-task analysis | **stage-wise, from evidence** |

**Revised gap statement (use in Introduction and Related Work).** Existing work establishes multi-table retrieval (MMQA, T-RAG), scalable multi-table QA over long contexts (TQA-Bench),
and cell-level attribution for open-domain answer tables (TANQ). What none of the resources we examined isolates is the effect of adding irrelevant relational tables to a fixed
question on evidence discovery and answer correctness, or whether the evidence a system retrieved is sufficient to reproduce the answer, or at which stage a failure occurs.

**Things the papers teach us (new risks and opportunities):**
1. **T-RAG's scale is far larger than ours** (60K tables vs 500) and it measures runtime. A reviewer will ask why our "scale" tops out at 500. Our defence is control, not size; but WP6 (1,000-2,000 tables) and a local retrieval-latency measurement (WP4) matter more now.
2. **T-RAG finds that a smaller model degrades with more tables** (LLaMA-3.1-8B drops going from 20 to 50 retrieved tables while GPT-4o improves). Our "full-schema is flat" finding is for two large models. **Add a small open model (e.g. an 8B model; `endor-text-qwen3-8b-latest` exists in the client)**: if it degrades, the story is richer and more defensible than a null result.
3. **MMQA sets the annotation bar**: two experts, a third for disagreements, agreement reported per task. WP2 should mirror it (two annotators + adjudicator, agreement per dimension).
4. **MMQA scores Text-to-SQL with ROUGE/BLEU and QA with a GPT-4 judge**; our deterministic execution-based scoring is a genuine, citable difference. Say it, with their metric names.
5. **TANQ's cell-level attribution** is the closest precedent for evidence provenance in open-domain answers (with HiTab for single tables); our difference is relational: join keys, complete qualifying sets, execution-verified sufficiency.
6. **A possible third source**: MMQA's released questions (Spider-based, with SQL and answers) or TQA-Bench's databases (DataGov, WorldBank are not BIRD/Spider). Both need a licence/overlap check against our decoy pool (Spider-train) before use.

## C. Checks already run in response (reproducible: `python eval/review_checks.py`, `build/09_completeness_probe.py`)
* **Not driven by a few databases:** BM25 decline appears in development databases (0.613 -> 0.349, -26.4) and held-out ones (0.755 -> 0.457, -29.8);
  23 of 31 databases decline, 7 unchanged, 1 improves; sign test over the 24 changed databases p ~ 1.5e-6; leave-one-database-out range -29.8 to -26.7.
  Full-schema: 2 decline, 25 unchanged, 4 improve (no effect).
* **Missed-table share by level** (BM25): 10.5% (+0), 26.0%, 38.5%, 46.5%, 49.0%, 51.0% (+500); pooled 36.9%; 71.8% of its wrong answers.
* **Value metric audit:** 108 of 6,000 SQL predictions (1.8%) are value-correct but strictly wrong; 107 differ only by column order, **1** is an accidental match.
* **Completeness probe:** 200/200 questions keep the gold answer in all 6 trials (5%, 25%, 50% of the other rows, 2 seeds) = 1,200 trials. Detects one omitted qualifying
  row ~87% of the time, several omitted rows almost always. It does NOT test whether the reference query itself is right (BIRD gold has ambiguity).
* **Table recall:** shown tables, not the full ranking: 0.951 (+0), 0.667 (+100), 0.595 (+500).

## D. Work packages
Effort: S < half a day, M ~ 1-2 days, L > 3 days. Owner: ME = I implement; YOU = needs your machine, credentials, or people.

### P0 — gates before submission
* **WP1 Positioning and related work (M; mostly done; remaining: MT-RAIG PDF and TQA-Bench venue).** Rewrite the gap as the reviewer suggests: controlled candidate-table cardinality, executable
  evidence sufficiency, stage-wise failure attribution (not "multi-table QA is new"). Extend Table 1 with MMQA, MT-RAIG, TANQ, and the published T-RAG, with cells verified from the papers.
  Done when: every Table 1 cell is traceable to a page. Remaining input: the MT-RAIG PDF (cited by T-RAG as a retrieval-over-multiple-tables benchmark) and the published TQA-Bench venue.
* **WP2 Annotation validity (M; YOU + 1-2 people, ME for analysis).** Mirror MMQA's protocol (two experts, a third resolves disagreements, agreement reported per dimension; MMQA reports 86% and 82%). (a) two annotators fill `data/annotation/sheet_annotator{A,B}.csv` (40 questions), I compute kappa per dimension
  and list corrected automatic labels as examples; (b) a second reviewer independently adjudicates the 33 alternatives (same CSV), report kappa; (c) paper defines three notions:
  *answer-sufficient* (strict sufficiency), *semantically complete* (completeness probe), *execution-path-specific* (strict sufficiency's reference-path dependence, relaxed by alternatives).
  Done when: agreement reported per dimension, with disagreements and corrections shown.
* **WP3 Retrieval baselines (M; ME builds, YOU runs).** Dense (embedding) table retrieval, hybrid BM25+dense (reciprocal-rank fusion), graph-aware (BM25 seeds expanded over foreign keys with personalised PageRank), plus existing BM25 and oracle.
  Cost is small (~1K tokens per prompt). **Decision needed: embedding source** (an embeddings endpoint on your gateway, or a local sentence-transformer you install). Graph-aware needs no embeddings.
* **WP4 Metrics and reporting fixes (S-M; ME, then YOU re-score).** Report **retrieval and index-build time locally** (deterministic; T-RAG reports runtime, and our LLM latency was unusable). Add `structured_accuracy` (column-permutation-insensitive row multiset) beside strict, set and value accuracy; report
  dev/held-out results; recall of tables actually shown; explicit failure-category definitions and denominators; real token counts (capture API usage or tokenize); per-database table in the supplement.

### P1 — strongly recommended
* **WP5 Decoy difficulty tiers (M; ME).** Tiers: random (current), schema-similar (high name/column similarity under the 0.5 Jaccard cap, character n-gram TF-IDF), semantically similar (embedding-based if available). Guarantee no alternative answer:
  keep the same-name and Jaccard<0.5 rules, and manually spot-check 50 variants per tier. Done when each tier has nested levels and passes the existing integrity checks. Expect BM25 to degrade more on the similar tiers; that comparison is the contribution.
* **WP6 Larger scale (L; YOU downloads, ME builds, YOU runs).** Levels 1,000 and 2,000 need more source tables: **download BIRD train databases** (adds ~69 databases). Run full-schema on a subset (token cost grows to ~107K per prompt at +1,000 and ~215K at +2,000).
  GPT-4o (128K context) should hit its limit near +1,000, directly answering "what happens past the context budget".
* **WP7 Coverage (L).** Questions 200 -> 500-1,000 from the remaining BIRD/Spider dev (all 2,568 already have automatic evidence), a third source if available (MMQA questions or TQA-Bench databases, after a licence and overlap check), 3-4 LLMs **including a small open model** (T-RAG shows 8B models degrade with more tables), 3 repeats where feasible.
  Full-schema across all six levels is ~104M estimated input tokens per model per 1,000 questions, so: full-schema on levels {0,100,500} only for the big set, other systems on all levels.
* **WP8 Variance (S; YOU runs, ME analyses).** `--no-cache --run-id r2/r3`, `eval/variance.py` (commands in RUNBOOK step 6).
* **WP9 Retrieve-and-read budgets (M).** Budgets 30/90/300 rows, dense and hybrid row retrieval, and a rows-structured answer prompt so the structured metric applies to non-SQL systems.

### P2 — quality
* **WP10 Presentation and release (M).** Benchmark-pipeline figure; title/abstract per review; acronym expansion; anonymized repository (rename `Stride_dataset`/`reltable`, remove internal client dependency and internal references); license checks for BIRD and Spider.
* **WP11 Paper structure under 12 pages (S).** Check whether PAKDD counts appendices. If not, move the per-database table, second-model table, and retrieve-and-read buckets to a supplement or the repository and keep five tables in the paper.

## E. Minimum viable revision vs full revision
* **Minimum viable (addresses all three dominant concerns):** WP1, WP2, WP3 (graph-aware + one embedding baseline), WP4, WP8, WP11. Result: defensible novelty, validated annotations, 3 retrieval families, variance.
* **Full:** add WP5, WP6, WP7, WP9, WP10. This is what moves the paper from "interesting pilot" to "benchmark".
* Cheapest high-value first order: WP4 -> WP8 -> WP2 -> WP3 -> WP1 -> WP5 -> WP6 -> WP7.

## F. Decisions and inputs needed
1. Submission deadline (sets minimum vs full).
2. Embedding source for dense/hybrid retrieval and semantic decoys.
3. Willingness to download BIRD train (needed for more than ~500 decoys).
4. Third source benchmark, if any, and which extra LLMs you can access.
5. People for the two-annotator validation.
6. The MT-RAIG PDF, and the published TQA-Bench venue.
7. Whether PAKDD counts appendices toward 12 pages.
