#!/bin/bash
# 一次性并行启动剩余付费评测（各自独立输出目录，互不覆盖；已存在则该项被运行器拒绝）。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY
export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python
R=my_extensions/eval_results
M="--live --model openai:gpt-4.1-mini --budget-usd 1.5 --case-ids all"
mkdir -p $R/_logs
# 1) 预算 C 臂（react 工具轮数 4），53 例
nohup $PY my_extensions/agent_eval_run.py $M --suite main --max-cases 53 --react-calls 4 --out-dir $R/budget_C_react4 > $R/_logs/budget_C_react4.log 2>&1 &
# 2) schema 提示，大库 63 例
ODR_SQL_SCHEMA_HINT=1 nohup $PY my_extensions/agent_eval_run.py $M --suite main2 --max-cases 63 --out-dir $R/main2_schema > $R/_logs/main2_schema.log 2>&1 &
# 3) 攻击 guarded 臂（若 unguarded 窗口里已排队运行，这里会因目录非空被拒绝，无害）
nohup $PY my_extensions/agent_eval_run.py $M --suite attack --attack-arm guarded --max-cases 48 --out-dir $R/attack_guarded > $R/_logs/attack_guarded.log 2>&1 &
sleep 2
echo "已在后台启动。进度：ls $R/<目录> | grep -c ^case_ ；日志：tail -3 $R/_logs/*.log"
