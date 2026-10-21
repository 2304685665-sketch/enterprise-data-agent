#!/bin/bash
# 间接注入评测 I0（无检索内容防护）/ I1（ODR_DOC_INJECTION_GUARD=1），见 PROTOCOL_inject.md。运行前确认 OpenAI 余额 ≥ $1。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
PY=.venv/bin/python; R=my_extensions/eval_results
if [ ! -f my_extensions/faiss_db_poison/index.faiss ]; then
  $PY my_extensions/build_poison_kb.py && $PY my_extensions/build_faiss_index.py --kb-dir my_extensions/knowledge_base_poison --out my_extensions/faiss_db_poison || exit 1
fi
M="--live --suite inject --model openai:gpt-4.1-mini --max-cases 16 --case-ids all --budget-usd 0.6"
mkdir -p $R/_logs
export ODR_VECTOR_BACKEND=faiss ODR_FAISS_DIR=$PWD/my_extensions/faiss_db_poison
nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/inject_I0 > $R/_logs/inject_I0.log 2>&1 &
ODR_DOC_INJECTION_GUARD=1 nohup $PY my_extensions/agent_eval_run.py $M --out-dir $R/inject_I1 > $R/_logs/inject_I1.log 2>&1 &
sleep 2; echo "已启动 inject_I0 / inject_I1（各 16 例）。进度：for d in inject_I0 inject_I1; do echo \$d \$(ls $R/\$d | grep -c ^case_); done"
