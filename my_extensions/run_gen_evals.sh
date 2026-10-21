#!/bin/bash
# 泛化检验 G0/G1/G2：三个臂并行（各自独立输出目录；已存在则被运行器拒绝，不会覆盖）。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
M="--live --suite gen --model openai:gpt-4.1-mini --max-cases 40 --case-ids all --budget-usd 0.6"
mkdir -p $R/_logs
nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/gen_G0 > $R/_logs/gen_G0.log 2>&1 &
ODR_SQL_SCHEMA_HINT=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/gen_G1 > $R/_logs/gen_G1.log 2>&1 &
ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/gen_G2 > $R/_logs/gen_G2.log 2>&1 &
sleep 2; echo "已启动 gen_G0 / gen_G1 / gen_G2（各 40 例）。进度：for d in gen_G0 gen_G1 gen_G2; do echo \$d \$(ls $R/\$d | grep -c ^case_); done"
