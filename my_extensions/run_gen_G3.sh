#!/bin/bash
# 条件臂 G3（PROTOCOL_generalization.md 事先声明：fast_D4 质量守门通过后追加）= D4 全量配置在新库新题上。
# 运行前请先确认 OpenAI 余额（预算上限 $0.35，运行器内置预算守卫）。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
M="--live --suite gen --model openai:gpt-4.1-mini --max-cases 40 --case-ids all --budget-usd 0.35"
mkdir -p $R/_logs
ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 ODR_SKIP_BRIEF=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/gen_G3 > $R/_logs/gen_G3.log 2>&1 &
sleep 2; echo "已启动 gen_G3（40 例）。进度：ls $R/gen_G3 | grep -c ^case_"
