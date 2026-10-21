#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results; M="--live --model openai:gpt-4.1-mini --budget-usd 1.5 --case-ids all"
mkdir -p $R/_logs
ODR_DIRECT_RESEARCH=1 nohup $PY my_extensions/agent_eval_run.py $M --suite main --max-cases 53 --out-dir $R/direct_main > $R/_logs/direct_main.log 2>&1 &
ODR_DIRECT_RESEARCH=1 nohup $PY my_extensions/agent_eval_run.py $M --suite main2 --max-cases 63 --out-dir $R/direct_main2 > $R/_logs/direct_main2.log 2>&1 &
sleep 2; echo "已启动 direct_main(53) 与 direct_main2(63)。进度：ls $R/direct_main | grep -c ^case_"
