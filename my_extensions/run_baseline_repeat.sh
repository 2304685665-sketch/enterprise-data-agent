#!/bin/bash
# 基线重复运行（PROTOCOL_repeat.md 补充）。每次约 $0.72，合计约 $1.5；运行前确认 OpenAI 余额 ≥ $2.5，并且 fault_F0/F1 已跑完。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
M="--live --suite main2 --model openai:gpt-4.1-mini --max-cases 63 --case-ids all --budget-usd 1.0"
mkdir -p $R/_logs
for n in r2 r3; do nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/base_main2_$n > $R/_logs/base_main2_$n.log 2>&1 & done
sleep 2; echo "已启动 base_main2_r2 / base_main2_r3（各 63 例）"
