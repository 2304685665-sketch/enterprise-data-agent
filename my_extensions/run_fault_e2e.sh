#!/bin/bash
# 端到端故障注入 F0/F1（PROTOCOL_fault_e2e.md）。运行前确认 OpenAI 余额 ≥ $1；需要已有 my_extensions/faiss_db_poison。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
[ -f my_extensions/faiss_db_poison/index.faiss ] || { echo "缺少 faiss_db_poison，先跑 run_inject_evals.sh 的建库步骤"; exit 1; }
M="--live --suite inject --model openai:gpt-4.1-mini --max-cases 16 --case-ids all --budget-usd 0.4 --fault-p 0.3 --fault-seed 20261014"
mkdir -p $R/_logs
export ODR_VECTOR_BACKEND=faiss ODR_FAISS_DIR=$PWD/my_extensions/faiss_db_poison ODR_DOC_INJECTION_GUARD=1
nohup $PY my_extensions/agent_eval_run.py $M --fault-retries 0 --out-dir $R/fault_F0 > $R/_logs/fault_F0.log 2>&1 &
nohup $PY my_extensions/agent_eval_run.py $M --fault-retries 2 --out-dir $R/fault_F1 > $R/_logs/fault_F1.log 2>&1 &
sleep 2; echo "已启动 fault_F0 / fault_F1（各 16 例）"
