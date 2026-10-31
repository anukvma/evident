"""Write a human-readable review sheet for the alternative-reference proposals: FULL gold and alternative SQL side by side, with
automatic hints (tables read, literals present in only one query). Hints help reading; they do not decide anything.

  python build/07b_alt_ref_review_sheet.py        ->  reports/alt_ref_review.md
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evident import paths, sqlast  # noqa: E402
from evident.evaluation import alt_refs  # noqa: E402
from evident.evaluation.gold import load_gold  # noqa: E402

LIT = re.compile(r"'((?:[^']|'')*)'|\"([^\"]*)\"|(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def literals(sql):
    out = set()
    for a, b, c in LIT.findall(re.sub(r"--[^\n]*", "", sql)):
        v = (a or b or c).strip().lower()
        if v and v not in {"0", "1", "1.0"}:
            out.add(v)
    return out


def main():
    data = json.load(open(alt_refs.PATH))
    gold = load_gold()
    an = {}
    lines = ["# Alternative-reference review sheet", "",
             "Decide per alternative: **confirm** only if the alternative is a reasonable derivation of the SAME answer from a different "
             "set of tables (e.g. skips a bridge table that adds no condition). **Reject** if it drops or adds a condition, uses a different "
             "relationship, or reaches the right answer by coincidence. Hints below are automatic and can be wrong.", ""]
    for q, d in data.items():
        g = gold[q]
        key = (g.source, g.db_id)
        if key not in an:
            an[key] = sqlast.Analyzer(sqlast.load_schema(paths.sqlite_path(*key))[0])
        gr = an[key].analyze(g.gold_sql).get("reads", {})
        lines += [f"## {q}  ({g.source}/{g.db_id}, {g.operation})", f"**Question:** {g.question}"]
        if g.knowledge:
            lines.append(f"**Knowledge:** {g.knowledge}")
        lines += ["", "**Gold SQL**", "```sql", " ".join(g.gold_sql.split()), "```", ""]
        gl = literals(g.gold_sql)
        for i, alt in enumerate(d["alternatives"]):
            ar = an[key].analyze(alt["sql"]).get("reads", {})
            al = literals(alt["sql"])
            lines += [f"### {q}:{i}   status={alt['status']}   seen in: {', '.join(s.split('[')[0] for s in alt.get('seen_in', []))}",
                      f"- tables only in gold: {sorted(set(gr) - set(ar)) or '-'};  only in alternative: {sorted(set(ar) - set(gr)) or '-'}",
                      f"- literals only in gold: {sorted(gl - al) or '-'};  only in alternative: {sorted(al - gl) or '-'}",
                      "```sql", " ".join(alt["sql"].split()), "```", ""]
    out = paths.REPORTS / "alt_ref_review.md"
    out.write_text("\n".join(lines))
    print(f"wrote {out} ({sum(len(d['alternatives']) for d in data.values())} alternatives, {len(data)} questions)")


if __name__ == "__main__":
    main()
