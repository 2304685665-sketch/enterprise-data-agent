#!/bin/bash
# D4 重复运行（PROTOCOL_repeat.md）：两个并行运行 fast_D4_r2 / fast_D4_r3，约 $0.5 合计。运行前确认 OpenAI 余额 ≥ $1。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
M="--live --suite main2 --model openai:gpt-4.1-mini --max-cases 63 --case-ids all --budget-usd 0.4"
D4="ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 ODR_SKIP_BRIEF=1"
mkdir -p $R/_logs
for n in r2 r3; do env $D4 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/fast_D4_$n > $R/_logs/fast_D4_$n.log 2>&1 & done
sleep 2; echo "已启动 fast_D4_r2 / fast_D4_r3（各 63 例）。进度：for d in fast_D4_r2 fast_D4_r3; do echo \$d \$(ls $R/\$d | grep -c ^case_); done"
