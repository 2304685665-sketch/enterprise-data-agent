#!/bin/bash
# 额度耗尽作废后的重跑与泛化检验，按优先级分档。用法: bash run_rerun.sh 1|2|3
# 档 1（约 $1.6）：fast_D4（主要比较）+ gen_G0/G1/G2（泛化检验）
# 档 2（约 $1.3）：attack_guarded_v2（防护臂）
# 档 3（约 $0.9）：fast_D2 + fast_D3（只作分解，不是主要比较）
# 启动前请先在 Billing 页面确认 Credit balance 足够本档估计值再加约 $0.5 余量；每次只启动一档。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results; mkdir -p $R/_logs
M2="--live --model openai:gpt-4.1-mini --budget-usd 1.0 --case-ids all --suite main2 --max-cases 63"
case "$1" in
1)
  ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 ODR_SKIP_BRIEF=1 nohup $PY my_extensions/agent_eval_run.py $M2 --out-dir $R/fast_D4 > $R/_logs/fast_D4.log 2>&1 &
  bash my_extensions/run_gen_evals.sh ;;
2)
  bash my_extensions/run_attack_guarded_rerun.sh ;;
3)
  ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 nohup $PY my_extensions/agent_eval_run.py $M2 --out-dir $R/fast_D2 > $R/_logs/fast_D2.log 2>&1 &
  ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 nohup $PY my_extensions/agent_eval_run.py $M2 --out-dir $R/fast_D3 > $R/_logs/fast_D3.log 2>&1 & ;;
*) echo "用法: bash run_rerun.sh 1|2|3"; exit 2 ;;
esac
sleep 2; echo "第 $1 档已在后台启动。进度：for d in \$(ls $R | grep -E '^(fast_D[234]|gen_G[012]|attack_guarded_v2)\$'); do echo \$d \$(ls $R/\$d | grep -c ^case_); done"
