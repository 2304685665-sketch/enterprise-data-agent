#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results; M="--live --model openai:gpt-4.1-mini --budget-usd 1.5 --case-ids all --suite main2 --max-cases 63"
mkdir -p $R/_logs
ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/fast_D2 > $R/_logs/fast_D2.log 2>&1 &
ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/fast_D3 > $R/_logs/fast_D3.log 2>&1 &
ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 ODR_SKIP_BRIEF=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/fast_D4 > $R/_logs/fast_D4.log 2>&1 &
sleep 2; echo "已启动 fast_D2/D3/D4。进度：for d in D2 D3 D4; do ls $R/fast_\$d | grep -c ^case_; done"
