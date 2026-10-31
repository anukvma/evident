#!/bin/sh
# Regenerate the paper tables/macros for the two-column ACL layout (run from reltable/).
set -e
OUT=../rexa_acl
python eval/make_paper_tables.py --final reports/eval_final_v6 --second reports/eval_final_gpt4o \
  --first-name "Gemini 2.5 Flash" --second-name "GPT-4o" --outdir $OUT --width '\columnwidth'
# the scale table spans both columns
sed -i '' 's/\\columnwidth/\\textwidth/; s/\\begin{table}\[t\]/\\begin{table*}[t]/; s/\\end{table}/\\end{table*}/' $OUT/tab_scale.tex
