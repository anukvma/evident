"""Generate LaTeX tables + number macros for the paper from a consolidated run (no hand-typed numbers).

  python eval/make_paper_tables.py --final reports/eval_final_v5 --outdir ../LaTeX2e+Proceedings+Template+ZIP
Writes results_macros.tex and tab_main.tex, tab_scale.tex, tab_failures.tex, tab_rr.tex; the paper \\input's them.
Re-run after any new scoring; every number in the prose is a macro defined here.
"""
import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from reltable.evaluation.gold import load_gold  # noqa: E402

NAMES = {"oracle_schema_t2s": "Oracle schema", "fullschema_t2s": "Full schema", "bm25_t2s": "BM25 top-5",
         "retrieve_read": "Retrieve-and-read", "question_blind": "Question-blind"}
SHORT = {"oracle_schema_t2s": "Oracle", "fullschema_t2s": "Full schema", "bm25_t2s": "BM25",
         "retrieve_read": "R\\&R", "question_blind": "Q-blind"}   # single-column tables
SQLSYS = ["oracle_schema_t2s", "fullschema_t2s", "bm25_t2s"]


def short(s):
    return s.split("[")[0]


def num(x):
    return not (x is None or (isinstance(x, float) and math.isnan(x)))


def f3(x):
    return f"{x:.3f}" if num(x) else "--"


def ci(d, nd=2):
    if d is None or d.get("mean") is None:
        return "--"
    s = f"{d['mean']:.3f}"
    if d.get("lo") is not None:
        s += f" [{d['lo']:.{nd}f}, {d['hi']:.{nd}f}]"
    return s


def pts(d, paren=True):
    t = f"95\\% CI {100 * d['lo']:+.1f} to {100 * d['hi']:+.1f}"
    return f"{100 * d['mean']:+.1f} points ({t})" if paren else f"{100 * d['mean']:+.1f} points; {t}"


def thou(x):
    return f"{x:,.0f}".replace(",", "{,}")


def mean(v):
    v = [x for x in v if num(x)]
    return sum(v) / len(v) if v else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", default=str(ROOT / "reports" / "eval_final_v5"))
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--width", default="\\textwidth", help="LaTeX width for resizebox (\\columnwidth for single-column ACL tables)")
    ap.add_argument("--second", default=None, help="consolidated run of a SECOND model (per_question.jsonl); adds tab_models.tex + macros")
    ap.add_argument("--first-name", default="Gemini 2.5 Flash"), ap.add_argument("--second-name", default="GPT-4o")
    a = ap.parse_args()
    summ = json.load(open(Path(a.final) / "summary.json"))
    rows = [json.loads(l) for l in open(Path(a.final) / "per_question.jsonl")]
    for r in rows:
        r["sys"] = short(r["system"])
    S = {short(k): v for k, v in summ["systems"].items()}
    LEVELS, N = summ["levels"], summ["n_questions"]
    top = str(max(LEVELS))
    R = lambda s, **kw: [r for r in rows if r["sys"] == s and all(r[k] == v for k, v in kw.items())]  # noqa: E731
    tables, mac = {}, []
    macro = lambda n, v: mac.append(f"\\newcommand{{\\{n}}}{{{v}}}")  # noqa: E731

    # ---------------------------------------------------------------- main table
    t = [r"""\begin{table}[t]
\caption{Gemini 2.5 Flash, %d questions $\times$ %d levels (+0 to +500), one run; means over all levels with 95\%% database-bootstrap intervals. BM25: BM25 top-5; R\&R: retrieve-and-read. Exec.: strict execution accuracy (SQL systems only). Suff.: strict evidence sufficiency; Suff.\ eq.\ also accepts human-confirmed alternative queries. Shown: recall of required tables among those placed in the prompt (R\&R: tables read). Tokens estimated as characters/4.}\label{tab:main-results}\centering\footnotesize\setlength{\tabcolsep}{3pt}
\begin{tabular}{lcccccr}\toprule
System & Value acc. & Exec. & Suff. & Suff.\ eq. & Shown & Tokens\\\midrule""" % (N, len(LEVELS))]
    for s in ["oracle_schema_t2s", "fullschema_t2s", "bm25_t2s", "retrieve_read"]:
        o, c = S[s]["overall"], S[s]["cost"]
        tok = "--" if c["input_tokens_mean"] is None else thou(c["input_tokens_mean"])
        va = "--" if s == "question_blind" else ci(o["value_accuracy"])
        ss = "--" if s == "question_blind" else ci(o["evidence_sufficient"])
        se = "--" if s == "question_blind" else ci(o["evidence_sufficient_eq"])
        ex = f3(o["exec_accuracy"]["mean"]) if o.get("exec_accuracy") else "--"
        se = f3(o["evidence_sufficient_eq"]["mean"])
        t.append(f"{SHORT[s]} & {va} & {ex} & {ss} & {se} & {f3(o['table_recall_selected']['mean'])} & {tok}\\\\")
    t.append(r"\bottomrule\end{tabular}\end{table}")
    tables["tab_main"] = "\n".join(t)

    # ---------------------------------------------------------------- scale table
    ncol = len(LEVELS) + 1
    t = [r"""\begin{table}[t]
\caption{Same questions, same data, same gold answers; only the number of added irrelevant tables changes (Gemini 2.5 Flash). $\Delta$: change from +0 to +%s, paired by question, 95\%% database-bootstrap interval. The oracle prompt is identical at every level, so its variation is sampling noise. Shown recall: required tables among the five in the prompt. Missed table: share of predictions whose prompt lacks a required table. Table R@10 is computed on the full ranking.}\label{tab:scale}\centering\footnotesize\setlength{\tabcolsep}{3pt}
\begin{tabular}{ll%s}\toprule
Metric & System & %s & $\Delta$ (+%s)\\\midrule""" % (top, "c" * ncol, " & ".join(f"+{l}" for l in LEVELS), top)]

    def block(title, key, systems, delta_key=None):
        for i, s in enumerate(systems):
            lv = [S[s]["by_level"][str(l)][key]["mean"] for l in LEVELS]
            d = "--"
            if delta_key and S[s].get("scale_degradation_vs_level0"):
                dd = S[s]["scale_degradation_vs_level0"][top].get(delta_key)
                d = ci(dd) if dd and dd.get("mean") is not None else "--"
            t.append(f"{title if i == 0 else ''} & {NAMES[s]} & " + " & ".join(f3(x) for x in lv) + f" & {d}\\\\")
        t.append(r"\midrule")
    block("Value acc.", "value_accuracy", SQLSYS + ["retrieve_read"], "value_accuracy")
    block("Evid. suff.", "evidence_sufficient", SQLSYS + ["retrieve_read"], "evidence_sufficient")
    block("Shown recall", "table_recall_selected", ["bm25_t2s"], "table_recall_selected")
    mt = [mean([float(r["failure"] == "missed_table") for r in R("bm25_t2s", level=l)]) for l in LEVELS]
    t.append(f"Missed table & {NAMES['bm25_t2s']} & " + " & ".join(f3(x) for x in mt) + f" & {mt[-1] - mt[0]:+.3f}\\\\")
    t.append(r"\midrule")
    block("Table R@10", "table_recall@10", ["bm25_t2s", "question_blind"], "table_recall@10")
    t.pop()   # no rule after the last block
    t.append(r"\bottomrule\end{tabular}\end{table}")
    tables["tab_scale"] = "\n".join(t)

    # ---------------------------------------------------------------- failure table
    labels = [("correct", "Correct"), ("missed_table", "Missed table"), ("sql_missed_column", "SQL col."),
              ("sql_incomplete_evidence", "SQL rows"), ("incomplete_evidence", "Incompl."),
              ("wrong_answer_despite_evidence", "Wrong, suff.")]
    t = [r"""\begin{table}[t]
\caption{Failure attribution: each prediction gets exactly one label, the first stage that fails, so rows sum to 100\%. Correct = strict execution accuracy (value accuracy for R\&R). Missed table: a required table was not in the prompt. SQL col./SQL rows: the table was shown but the system's own query skipped a required column or qualifying rows (planning). Incompl.: the rows read cannot reproduce the answer. Wrong, suff.: sufficient evidence, wrong answer. Other: join key, query failure, refusal. Gemini: all six levels; GPT-4o: +0 to +100.}\label{tab:failures}\centering\footnotesize\setlength{\tabcolsep}{3pt}
\begin{tabular}{ll""" + "c" * (len(labels) + 1) + r"""}\toprule
Model & System & """ + " & ".join(l for _, l in labels) + r" & Other\\\midrule"]
    for s in SQLSYS + ["retrieve_read"]:
        fb = S[s]["failure_breakdown"]
        share = lambda k: fb.get(k, {"share": 0})["share"]  # noqa: E731
        other = 1 - sum(share(k) for k, _ in labels)
        mname = a.first_name.split()[0] if s == SQLSYS[0] else ""
        t.append(f"{mname} & {SHORT[s]} & " + " & ".join(f"{100 * share(k):.1f}\\%" for k, _ in labels) + f" & {100 * other:.1f}\\%\\\\")
    fail_rows, fail_labels = t, labels   # closed after the optional second model adds its rows

    # ---------------------------------------------------------------- retrieve-and-read by rows needed
    gold = load_gold()
    need = {q: sum(len(v) for v in g.row_ids.values()) for q, g in gold.items()}
    bucket = lambda n: "$\\le$30" if n <= 30 else "31--90" if n <= 90 else "91--1000" if n <= 1000 else "$>$1000"  # noqa: E731
    order = ["$\\le$30", "31--90", "91--1000", "$>$1000"]
    t = [r"""\begin{table}[t]
\caption{Retrieve-and-read (R\&R, no SQL) by the evidence rows a question needs; it reads at most 90 (3 tables $\times$ 30). Level +0 only. Suff.: the rows read reproduce the gold answer. Acc.: value accuracy; BM25: the BM25 top-5 text-to-SQL pipeline on the same questions.}\label{tab:rr}\centering\footnotesize\setlength{\tabcolsep}{3pt}
\begin{tabular}{lcccc}\toprule
Rows needed & Qs & R\&R suff. & R\&R acc. & BM25 acc.\\\midrule"""]
    rr0, bm0 = R("retrieve_read", level=0), R("bm25_t2s", level=0)
    rr_bm_by_bucket = {}
    for b in order:
        rq = [r for r in rr0 if bucket(need[r["qid"]]) == b]
        bq = [r for r in bm0 if bucket(need[r["qid"]]) == b]
        t.append(f"{b} & {len(rq)} & {f3(mean([r['evidence_sufficient'] for r in rq]))} & {f3(mean([r['value_accuracy'] for r in rq]))} & {f3(mean([r['value_accuracy'] for r in bq]))}\\\\")
        rr_bm_by_bucket[b] = mean([r["value_accuracy"] for r in bq])
    t.append(r"\bottomrule\end{tabular}\end{table}")
    tables["tab_rr"] = "\n".join(t)

    # ---------------------------------------------------------------- macros
    p0 = lambda x: f"{100 * x:.0f}\\%"  # noqa: E731
    macro("orValueP", p0(S["oracle_schema_t2s"]["overall"]["value_accuracy"]["mean"]))
    macro("bmValueZeroP", p0(S["bm25_t2s"]["by_level"]["0"]["value_accuracy"]["mean"]))
    macro("bmValueTopP", p0(S["bm25_t2s"]["by_level"][top]["value_accuracy"]["mean"]))
    macro("bmMissedTableP", p0(S["bm25_t2s"]["failure_breakdown"]["missed_table"]["share"]))
    _mz = mean([float(r["failure"] == "missed_table") for r in R("bm25_t2s", level=0)])
    _mt = mean([float(r["failure"] == "missed_table") for r in R("bm25_t2s", level=int(top))])
    macro("bmMissedZeroP", p0(_mz)); macro("bmMissedTopP", p0(_mt))
    _fs = S["fullschema_t2s"]["scale_degradation_vs_level0"][top]["value_accuracy"]
    macro("fsCiPoints", f"95\\% CI ${100 * _fs['lo']:+.0f}$ to ${100 * _fs['hi']:+.0f}$ points")
    macro("rrSuffP", p0(mean([r["value_accuracy"] for r in R("retrieve_read", level=0) if r["evidence_sufficient"] == 1.0])))
    macro("rrNotSuffP", p0(mean([r["value_accuracy"] for r in R("retrieve_read", level=0) if r["evidence_sufficient"] == 0.0])))
    macro("nQuestions", str(N)); macro("topLevel", top); macro("nLevels", str(len(LEVELS)))
    macro("nPredictions", str(len(R("bm25_t2s"))))
    sd = lambda s, l, m="value_accuracy": S[s]["scale_degradation_vs_level0"][str(l)][m]  # noqa: E731
    macro("bmValueZero", f3(S["bm25_t2s"]["by_level"]["0"]["value_accuracy"]["mean"]))
    macro("bmValueTop", f3(S["bm25_t2s"]["by_level"][top]["value_accuracy"]["mean"]))
    macro("bmDeltaTop", pts(sd("bm25_t2s", top)))
    macro("bmDeltaAbsTop", f"{abs(100 * sd('bm25_t2s', top)['mean']):.1f}")
    macro("bmDeltaHundred", pts(sd("bm25_t2s", 100)))
    macro("fsDeltaTop", pts(sd("fullschema_t2s", top), paren=False))
    macro("fsDeltaMid", pts(sd("fullschema_t2s", 250), paren=False))
    macro("orDeltaTop", pts(sd("oracle_schema_t2s", top)))
    for tag, s in (("bm", "bm25_t2s"), ("fs", "fullschema_t2s"), ("or", "oracle_schema_t2s")):
        macro(f"{tag}Value", f3(S[s]["overall"]["value_accuracy"]["mean"]))
    macro("rrValue", f3(S["retrieve_read"]["overall"]["value_accuracy"]["mean"]))
    macro("rrValueZero", f3(S["retrieve_read"]["by_level"]["0"]["value_accuracy"]["mean"]))
    macro("bmValueAtZero", f3(S["bm25_t2s"]["by_level"]["0"]["value_accuracy"]["mean"]))
    macro("bmRecallZero", f3(S["bm25_t2s"]["by_level"]["0"]["table_recall@10"]["mean"]))
    macro("bmRecallTop", f3(S["bm25_t2s"]["by_level"][top]["table_recall@10"]["mean"]))
    macro("qbRecallZero", f3(S["question_blind"]["by_level"]["0"]["table_recall@10"]["mean"]))
    macro("qbRecallTop", f3(S["question_blind"]["by_level"][top]["table_recall@10"]["mean"]))
    macro("bmSuffZero", f3(S["bm25_t2s"]["by_level"]["0"]["evidence_sufficient"]["mean"]))
    macro("bmSuffTop", f3(S["bm25_t2s"]["by_level"][top]["evidence_sufficient"]["mean"]))
    macro("bmMissedTable", f"{100 * S['bm25_t2s']['failure_breakdown']['missed_table']['share']:.1f}\\%")
    tk = lambda s, l: mean([r["cost_input_tokens"] for r in R(s, level=l)])  # noqa: E731
    macro("fsTokZero", thou(tk("fullschema_t2s", 0))); macro("fsTokTop", thou(tk("fullschema_t2s", int(top))))
    macro("bmTokTop", thou(tk("bm25_t2s", int(top))))
    macro("tokRatioTop", f"{tk('fullschema_t2s', int(top)) / tk('bm25_t2s', int(top)):.0f}")
    bs = {s: {src: mean([r["value_accuracy"] for r in R(s, source=src)]) for src in ("bird", "spider")} for s in SQLSYS}
    macro("orBird", f3(bs["oracle_schema_t2s"]["bird"])); macro("orSpider", f3(bs["oracle_schema_t2s"]["spider"]))
    ops = S["oracle_schema_t2s"]["by_operation"]
    macro("orNested", f3(ops["nested"]["value_accuracy"]["mean"])); macro("orLookup", f3(ops["lookup"]["value_accuracy"]["mean"]))
    nq = {k: len({r["qid"] for r in rows if r["n_tables"] == k}) for k in (1, 2, 3, 4)}
    macro("nFourTables", str(nq[4]))
    for s, tag in (("oracle_schema_t2s", "or"), ("fullschema_t2s", "fs"), ("bm25_t2s", "bm")):
        bt = S[s]["by_n_tables"]
        macro(f"{tag}TOne", f3(bt["1"]["value_accuracy"]["mean"])); macro(f"{tag}TFour", f3(bt["4"]["value_accuracy"]["mean"]))
    # set vs multiset
    gains = {s: 100 * (mean([r["exec_accuracy_set"] for r in R(s)]) - mean([r["exec_accuracy"] for r in R(s)])) for s in SQLSYS}
    macro("setGainMin", f"{min(gains.values()):.1f}"); macro("setGainMax", f"{max(gains.values()):.1f}")
    macro("orSetBird", f3(mean([r["exec_accuracy_set"] for r in R("oracle_schema_t2s", source="bird")])))
    bsl = lambda l: mean([r["exec_accuracy_set"] for r in R("bm25_t2s", level=l)])  # noqa: E731
    macro("bmSetDeltaPts", f"{100 * (bsl(int(top)) - bsl(0)):+.1f}")
    # correct answers whose evidence is 'insufficient': strict vs equivalence-aware
    for s, tag in (("bm25_t2s", "bm"), ("fullschema_t2s", "fs"), ("oracle_schema_t2s", "or")):
        cor = [r for r in R(s) if r["answer_correct"] == 1.0 and num(r["evidence_sufficient"])]
        macro(f"{tag}CorrectInsuff", f"{100 * mean([float(r['evidence_sufficient'] != 1.0) for r in cor]):.1f}\\%")
        macro(f"{tag}CorrectInsuffEq", f"{100 * mean([float(r['evidence_sufficient_eq'] != 1.0) for r in cor]):.1f}\\%")
        allr = [r for r in R(s) if num(r["evidence_sufficient"])]
        macro(f"{tag}WrongSuff", f"{100 * mean([float(r['answer_correct'] == 0 and r['evidence_sufficient'] == 1.0) for r in allr]):.1f}\\%")
    # retrieve-and-read diagnostics (level 0)
    suff = [r for r in rr0 if r["evidence_sufficient"] == 1.0]
    nots = [r for r in rr0 if r["evidence_sufficient"] == 0.0]
    macro("rrSuffAcc", f3(mean([r["value_accuracy"] for r in suff]))); macro("rrSuffN", str(len(suff)))
    macro("rrNotSuffAcc", f3(mean([r["value_accuracy"] for r in nots]))); macro("rrNotSuffN", str(len(nots)))
    big = [r for r in rr0 if need[r["qid"]] > 90]
    pct = lambda x: f"{100 * x:.1f}\\%"  # noqa: E731
    macro("rrBigAccPct", pct(mean([r["value_accuracy"] for r in big]))); macro("rrBigSuffPct", pct(mean([r["evidence_sufficient"] for r in big])))
    macro("bmBigAccPct", pct(mean([r["value_accuracy"] for r in bm0 if need[r["qid"]] > 90])))
    macro("rrBigN", str(len(big))); macro("rrBigAcc", f3(mean([r["value_accuracy"] for r in big])))
    macro("rrBigSuff", f3(mean([r["evidence_sufficient"] for r in big])))
    macro("bmBigAcc", f3(mean([r["value_accuracy"] for r in bm0 if need[r["qid"]] > 90])))
    small = [r for r in rr0 if need[r["qid"]] <= 30]
    macro("rrSmallAccPct", pct(mean([r["value_accuracy"] for r in small]))); macro("rrSmallSuffPct", pct(mean([r["evidence_sufficient"] for r in small])))
    macro("rrSuffAccPct", pct(mean([r["value_accuracy"] for r in suff]))); macro("rrNotSuffAccPct", pct(mean([r["value_accuracy"] for r in nots])))
    macro("rrSmallN", str(len(small))); macro("rrSmallAcc", f3(mean([r["value_accuracy"] for r in small])))
    macro("rrSmallSuff", f3(mean([r["evidence_sufficient"] for r in small])))
    macro("bmRowsSmallAcc", f3(rr_bm_by_bucket[order[0]])); macro("bmRowsHugeAcc", f3(rr_bm_by_bucket[order[-1]]))
    macro("nCompleteSet", str(sum(1 for g in gold.values() if g.rows_mode == "complete_set")))
    macro("nOverFiveThousand", str(sum(1 for n in need.values() if n > 5000)))
    macro("nOverNinety", str(sum(1 for n in need.values() if n > 90)))
    # latency is NOT comparable across levels (gateway load drifted between runs)
    lat = lambda s, l: st.median([r["cost_latency_s"] for r in R(s, level=l) if num(r["cost_latency_s"])])  # noqa: E731
    macro("orLatZero", f"{lat('oracle_schema_t2s', 0):.1f}"); macro("orLatTop", f"{lat('oracle_schema_t2s', int(top)):.1f}")
    byq = {}
    for r in R("oracle_schema_t2s"):
        byq.setdefault(r["qid"], set()).add(r["value_accuracy"])
    macro("orFlips", str(sum(len(v) > 1 for v in byq.values())))
    macro("nSysErr", str(sum(1 for r in rows if r["exec_status"] == "system_error")))
    macro("nSysErrSystems", ", ".join(sorted({r["sys"] for r in rows if r["exec_status"] == "system_error"})) or "none")

    if a.second:
        import compare_models as CM  # eval/ is on sys.path when run as a script
        A, B = CM.load(a.final), CM.load(a.second)
        common = sorted({r["level"] for r in A} & {r["level"] for r in B}); ctop = max(common)
        res = {}
        for name, Rr in ((a.first_name, A), (a.second_name, B)):
            res[name] = {}
            for sysn in SQLSYS:
                rs = [r for r in Rr if r["sys"] == sysn and r["level"] in common]
                lv = {l: mean([r["value_accuracy"] for r in rs if r["level"] == l]) for l in common}
                by0 = {r["qid"]: r["value_accuracy"] for r in rs if r["level"] == 0 and num(r["value_accuracy"])}
                dd = [(r["db"], r["value_accuracy"] - by0[r["qid"]]) for r in rs
                      if r["level"] == ctop and num(r["value_accuracy"]) and r["qid"] in by0]
                m_, lo_, hi_, _ = CM.boot(dd)
                res[name][sysn] = {"lv": lv, "d": (m_, lo_, hi_), "overall": mean([r["value_accuracy"] for r in rs]),
                                   "ref": sum(1 for r in rs if r["exec_status"] == "no_answer") / len(rs),
                                   "exec": mean([r["exec_accuracy"] for r in rs]),
                                   "suff": mean([r["evidence_sufficient"] for r in rs]),
                                   "missed": mean([float(r["failure"] == "missed_table") for r in rs]),
                                   "fail": {k: mean([float(r["failure"] == k) for r in rs]) for k in {r["failure"] for r in rs}},
                                   "tok": mean([r["cost_input_tokens"] for r in rs if r["level"] == ctop])}
        t = [r"""\begin{table}[t]
\caption{Both models on byte-identical prompts over the shared levels (+0 to +%d): value accuracy by number of added tables; $\Delta$: change from +0 to +%d, paired by question, 95\%% database-bootstrap interval. Suff.: strict evidence sufficiency; Missed: predictions whose prompt lacks a required table; Ref.: predictions declining to write a query (counted wrong). Suff., Missed and Ref.\ are pooled over the shared levels.}\label{tab:models}\centering\footnotesize\setlength{\tabcolsep}{3pt}
\begin{tabular}{ll%scccc}\toprule
System & Model & %s & $\Delta$ (+%d) & Suff. & Missed & Ref.\\\midrule""" % (ctop, ctop, "c" * len(common), " & ".join(f"+{l}" for l in common), ctop)]
        for sysn in SQLSYS:
            for i, name in enumerate((a.first_name, a.second_name)):
                x = res[name][sysn]
                t.append(f"{SHORT[sysn] if i == 0 else ''} & {name.split()[0]} & " + " & ".join(f3(x['lv'][l]) for l in common) +
                         f" & {x['d'][0]:+.3f} [{x['d'][1]:+.2f}, {x['d'][2]:+.2f}] & {f3(x['suff'])} & {100 * x['missed']:.1f}\\% & {100 * x['ref']:.1f}\\%\\\\")
            t.append(r"\midrule")
        t.pop()
        t.append(r"\bottomrule\end{tabular}\end{table}")
        tables["tab_models"] = "\n".join(t)
        g = a.second_name
        for sysn, tag in (("oracle_schema_t2s", "Or"), ("fullschema_t2s", "Fs"), ("bm25_t2s", "Bm")):
            macro(f"gp{tag}Value", f3(res[g][sysn]["overall"]))
            macro(f"gp{tag}Delta", f"{100 * res[g][sysn]['d'][0]:+.1f} points; 95\\% CI {100 * res[g][sysn]['d'][1]:+.1f} to {100 * res[g][sysn]['d'][2]:+.1f}")
        f_ = a.first_name
        for sysn, tag in (("oracle_schema_t2s", "Or"), ("fullschema_t2s", "Fs"), ("bm25_t2s", "Bm")):
            macro(f"gm{tag}ValueCommon", f3(res[f_][sysn]["overall"]))
            ia = {(r["qid"], r["level"]): r["value_accuracy"] for r in A if r["sys"] == sysn and r["level"] in common and num(r["value_accuracy"])}
            dd = [(r["db"], r["value_accuracy"] - ia[(r["qid"], r["level"])]) for r in B
                  if r["sys"] == sysn and (r["qid"], r["level"]) in ia and num(r["value_accuracy"])]
            m_, lo_, hi_, _ = CM.boot(dd)
            macro(f"gpDiff{tag}", f"{100 * m_:+.1f} points; 95\\% CI {100 * lo_:+.1f} to {100 * hi_:+.1f}")
            macro(f"gpDiff{tag}Sig", "significant" if (lo_ > 0 or hi_ < 0) else "not significant")
        macro("gpBmDeltaAbs", f"{abs(100 * res[g]['bm25_t2s']['d'][0]):.1f}")
        macro("gpBmRefusals", f"{100 * res[g]['bm25_t2s']['ref']:.1f}\\%")
        # second-model rows in the failure table
        for i, sysn in enumerate(SQLSYS):
            fb = res[g][sysn]["fail"]
            other = 1 - sum(fb.get(k, 0) for k, _ in fail_labels)
            fail_rows.append((r"\midrule" + "\n" if i == 0 else "") + f"{g.split()[0] if i == 0 else ''} & {SHORT[sysn]} & " +
                             " & ".join(f"{100 * fb.get(k, 0):.1f}\\%" for k, _ in fail_labels) + f" & {100 * other:.1f}\\%\\\\")
        P = lambda x: f"{100 * x:.1f}\\%"  # noqa: E731
        for name, mt in ((f_, "gm"), (g, "gp")):
            for sysn, tag in (("oracle_schema_t2s", "Or"), ("fullschema_t2s", "Fs"), ("bm25_t2s", "Bm")):
                macro(f"{mt}{tag}SuffCommon", f3(res[name][sysn]["suff"]))
                macro(f"{mt}{tag}ExecCommon", f3(res[name][sysn]["exec"]))
                macro(f"{mt}{tag}ZeroCommon", f3(res[name][sysn]["lv"][0]))
                macro(f"{mt}{tag}TopCommon", f3(res[name][sysn]["lv"][ctop]))
            macro(f"{mt}BmMissedCommon", P(res[name]["bm25_t2s"]["missed"]))
            macro(f"{mt}FsTokCommonTop", thou(res[name]["fullschema_t2s"]["tok"]))
            rb = [r for r in (A if name == f_ else B) if r["sys"] == "bm25_t2s"]
            macro(f"{mt}BmMissedZeroP", p0(mean([float(r["failure"] == "missed_table") for r in rb if r["level"] == 0])))
            macro(f"{mt}BmMissedCommonTopP", p0(mean([float(r["failure"] == "missed_table") for r in rb if r["level"] == ctop])))
        macro("gpNPredictions", thou(len(B)))
        macro("gpCommonTop", str(ctop))
        orders = {n: sorted(SQLSYS, key=lambda s_: -res[n][s_]["overall"]) for n in res}
        macro("gpSameOrder", "identically" if orders[a.first_name] == orders[a.second_name] else "differently")
        fs_flat = res[g]["fullschema_t2s"]["d"][1] <= 0 <= res[g]["fullschema_t2s"]["d"][2]
        bm_down = res[g]["bm25_t2s"]["d"][2] < 0
        macro("gpPatternHolds", "holds" if (fs_flat and bm_down) else "does not fully hold")
    fail_rows.append(r"\bottomrule\end{tabular}\end{table}")
    tables["tab_failures"] = "\n".join(fail_rows)

    # ---------------------------------------------------------------- robustness checks (eval/review_checks.py)
    rc_path = ROOT / "reports" / "review_checks.json"
    if rc_path.exists():
        rc = json.load(open(rc_path))
        sp = rc["split"]
        for spl, tg in (("dev", "Dev"), ("test", "Test")):
            for sysn, tag in (("bm25_t2s", "bm"), ("fullschema_t2s", "fs"), ("oracle_schema_t2s", "or")):
                z, h = sp[f"{spl}:{sysn}"]["zero"], sp[f"{spl}:{sysn}"]["top"]
                macro(f"{tag}{tg}Zero", f3(z)); macro(f"{tag}{tg}Top", f3(h)); macro(f"{tag}{tg}Delta", f"{100 * (h - z):+.1f}")
            macro(f"n{tg}Db", str(sp[f"{spl}:bm25_t2s"]["n_db"]))
            macro(f"n{tg}Q", str(len({r["qid"] for r in rows if r.get("split") == spl})))
        for sysn, tag in (("bm25_t2s", "bm"), ("fullschema_t2s", "fs")):
            pd = rc[f"perdb:{sysn}"]
            macro(f"{tag}DbDecline", str(pd["decline"])); macro(f"{tag}DbSame", str(pd["same"])); macro(f"{tag}DbImprove", str(pd["improve"]))
            macro(f"{tag}LodoMin", f"{100 * pd['lodo_min']:+.1f}"); macro(f"{tag}LodoMax", f"{100 * pd['lodo_max']:+.1f}")
            mant, ex = f"{pd['sign_p']:.1e}".split("e")
            macro(f"{tag}SignP", f"${mant}\\times10^{{{int(ex)}}}$" if int(ex) < -2 else f"{pd['sign_p']:.2f}")
        va = rc["value_audit"]
        macro("nSqlPredictions", thou(rc["n_sql_predictions"])); macro("nValueOnly", str(va["value_ok_strict_wrong"]))
        macro("nValueColOrder", str(va["column_order_only"])); macro("nValueAccidental", str(va["structure_differs"]))
        cp = rc["completeness"]
        macro("nProbeTrials", thou(cp["trials"])); macro("nProbeChanged", str(cp["changed"]))
    for l, w in zip(LEVELS[1:], ["Ten", "Fifty", "Hundred", "TwoFifty", "FiveHundred"]):   # +0 is bmMissedZeroP above
        macro(f"bmMissed{w}P", p0(mean([float(r["failure"] == "missed_table") for r in R("bm25_t2s", level=l)])))
    for l, w in ((0, "Zero"), (100, "Hundred"), (int(top), "Top")):
        macro(f"bmShown{w}", f3(S["bm25_t2s"]["by_level"][str(l)]["table_recall_selected"]["mean"]))
    for s_, tag in (("oracle_schema_t2s", "or"), ("fullschema_t2s", "fs"), ("bm25_t2s", "bm")):
        macro(f"{tag}Exec", f3(S[s_]["overall"]["exec_accuracy"]["mean"]))
    macro("bmMissedOfWrongP", p0(mean([float(r["failure"] == "missed_table") for r in R("bm25_t2s") if r["answer_correct"] == 0.0])))

    # ---------------------------------------------------------------- pilot composition
    q0 = {r["qid"]: r for r in R("oracle_schema_t2s", level=0)}
    srcs = [("bird", "BIRD"), ("spider", "Spider")]
    ncand = lambda l: [r["n_candidate_tables"] for r in R("bm25_t2s", level=l)]  # noqa: E731
    t = [r"""\begin{table}[t]
\caption{Pilot composition. Tables read: tables the reference query reads. Complete set: questions whose evidence is the whole qualifying set (aggregation, ranking, counting, negation). Rows needed: gold evidence rows. Candidates: tables a system can see at +0 and at +%s.}\label{tab:stats}\centering\footnotesize\setlength{\tabcolsep}{3pt}
\begin{tabular}{lccccccccc}\toprule
 & Qs & DBs & \multicolumn{4}{c}{Tables read} & Complete & Rows needed & Candidates\\
 & & & 1 & 2 & 3 & 4+ & set & median / max & +0 / +%s\\\midrule""" % (top, top)]
    for src, lab in srcs + [(None, "Total")]:
        qs = [r for r in q0.values() if src is None or r["source"] == src]
        nt = lambda k: sum(1 for r in qs if (r["n_tables"] >= 4 if k == 4 else r["n_tables"] == k))  # noqa: E731
        nd = sorted(need[r["qid"]] for r in qs)
        c0 = [r["n_candidate_tables"] for r in R("bm25_t2s", level=0) if src is None or r["source"] == src]
        cT = [r["n_candidate_tables"] for r in R("bm25_t2s", level=int(top)) if src is None or r["source"] == src]
        t.append((r"\midrule" + "\n" if src is None else "") +
                 f"{lab} & {len(qs)} & {len({r['db'] for r in qs})} & {nt(1)} & {nt(2)} & {nt(3)} & {nt(4)} & "
                 f"{sum(1 for r in qs if r['rows_mode'] == 'complete_set')} & {thou(st.median(nd))} / {thou(max(nd))} & "
                 f"{min(c0)}--{max(c0)} / {min(cT)}--{max(cT)}\\\\")
    t.append(r"\bottomrule\end{tabular}\end{table}")
    tables["tab_stats"] = "\n".join(t)
    ops = {}
    for r in q0.values():
        ops[r["operation"]] = ops.get(r["operation"], 0) + 1
    for k, v in ops.items():
        macro(f"nOp{k.capitalize()}", str(v))
    macro("nFourBird", str(sum(1 for r in q0.values() if r["n_tables"] >= 4 and r["source"] == "bird")))
    macro("nCandZeroMax", str(max(ncand(0)))); macro("nCandTopMax", str(max(ncand(int(top)))))
    for s_, tag in (("oracle_schema_t2s", "or"), ("bm25_t2s", "bm")):
        for k, w in ((1, "One"), (4, "Four")):
            rs = [r for r in R(s_, source="bird") if (r["n_tables"] >= 4 if k == 4 else r["n_tables"] == k)]
            macro(f"{tag}BirdT{w}", f3(mean([r["value_accuracy"] for r in rs])))

    d = Path(a.outdir)
    # ---------------------------------------------------------------- numbers from one-off review files (previously hand-typed)
    rep = ROOT / "reports"
    if (rep / "alt_ref_review.csv").exists():
        import csv
        alt = list(csv.DictReader(open(rep / "alt_ref_review.csv")))
        macro("nAltReviewed", str(len(alt))); macro("nAltConfirmed", str(sum(r["your_decision"] == "confirm" for r in alt)))
    if (rep / "side_analyses.json").exists():
        sa = json.load(open(rep / "side_analyses.json"))
        lc = sa["bm25_missed_table_label_check"]
        macro("nMissedHandChecked", str(lc["labelled_missed_table_at_L100"])); macro("nMissedHandConfirmed", str(lc["required_table_truly_absent_from_prompt"]))
        sc = sa["correct_but_insufficient_manual_review_oracle"]
        macro("nOrShortcutCases", str(sc["distinct_questions"])); macro("nOrShortcutValid", str(len(sc["valid_shortcut_equivalent_path"])))
    if (rep / "shortcut_probe.json").exists():
        sp_ = json.load(open(rep / "shortcut_probe.json"))
        macro("probeAucBird", f"{sp_['decoy_vs_real']['bird']['size+structure+style']:.2f}")
        macro("probeAucAll", f"{sp_['decoy_vs_real']['all']['size+structure+style']:.2f}")
    tok_sum = lambda rs: sum(r["cost_input_tokens"] or 0 for r in rs)  # noqa: E731
    sql_tok = {s_: tok_sum(R(s_)) for s_ in SQLSYS}
    macro("tokSqlRunM", f"{sum(sql_tok.values()) / 1e6:.1f}")
    macro("tokSqlFsShareP", p0(sql_tok["fullschema_t2s"] / sum(sql_tok.values())))
    macro("tokRrRunM", f"{tok_sum(R('retrieve_read')) / 1e6:.1f}")
    if a.second:
        macro("tokGpRunM", f"{tok_sum(B) / 1e6:.1f}")
    (d / "results_macros.tex").write_text("% AUTO-GENERATED by eval/make_paper_tables.py from " + str(a.final) + ". Do not edit.\n" + "\n".join(mac) + "\n")
    for name, body in tables.items():
        body = body.replace("\\begin{tabular}", "\\resizebox{" + a.width + "}{!}{\\begin{tabular}", 1).replace("\\end{tabular}", "\\end{tabular}}", 1)
        if name == "tab_models":                      # one rule under the header only, to save vertical space
            parts = body.split("\\midrule")
            body = parts[0] + "\\midrule" + "".join(parts[1:])
        (d / f"{name}.tex").write_text("% AUTO-GENERATED by eval/make_paper_tables.py. Do not edit.\n" + body + "\n")
    print("\n".join(mac))
    print(f"\nwrote results_macros.tex and {len(tables)} tables to {d}")


if __name__ == "__main__":
    main()
