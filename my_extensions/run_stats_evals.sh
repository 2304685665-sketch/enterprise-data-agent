#!/bin/bash
# 统计函数留出实验 S0/S1（PROTOCOL_stats_funcs.md）。运行前先确认 OpenAI 余额 ≥ $1。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
M="--live --suite stats --model openai:gpt-4.1-mini --max-cases 12 --case-ids all --budget-usd 0.25"
D4="ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 ODR_SKIP_BRIEF=1"
mkdir -p $R/_logs
env $D4 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/stats_S0 > $R/_logs/stats_S0.log 2>&1 &
env $D4 ODR_SQL_STATS_FUNCS=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/stats_S1 > $R/_logs/stats_S1.log 2>&1 &
sleep 2; echo "已启动 stats_S0 / stats_S1（各 12 例）。进度：for d in stats_S0 stats_S1; do echo \$d \$(ls $R/\$d | grep -c ^case_); done"
