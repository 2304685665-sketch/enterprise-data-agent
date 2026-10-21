#!/bin/bash
# 重跑 attack_guarded（第一次因相对路径 bug 作废，见 EVIDENCE §40）。预计 $1.2–1.4，预算上限 $1.5。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
mkdir -p my_extensions/eval_results/_logs
nohup .venv/bin/python my_extensions/agent_eval_run.py --live --suite attack --attack-arm guarded --model openai:gpt-4.1-mini --max-cases 48 --case-ids all --budget-usd 1.5 --out-dir my_extensions/eval_results/attack_guarded_v2 > my_extensions/eval_results/_logs/attack_guarded_v2.log 2>&1 &
sleep 2; echo "已启动 attack_guarded_v2。进度：ls my_extensions/eval_results/attack_guarded_v2 | grep -c ^case_"
