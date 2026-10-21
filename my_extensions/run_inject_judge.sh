#!/bin/bash
# 检索层 LLM 判别评测（PROTOCOL_inject_large.md 补充），共两次运行，约 $0.1。运行前确认 OpenAI 余额 ≥ $1。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
R=my_extensions/inject_results
for n in 1 2; do .venv/bin/python my_extensions/eval_inject_large.py --judge --out $R/offline_large_judge_run${n}_$(date +%Y%m%d).json > $R/_judge_run$n.txt 2>&1; echo "run $n 完成"; tail -25 $R/_judge_run$n.txt; done
