#!/bin/bash
# LLM 判别层评测（memory_filter_audit/PROTOCOL_memory_judge.md）。约 $0.05；运行前确认 OpenAI 余额 ≥ $1。
cd "$(dirname "$0")/.." || exit 1
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
.venv/bin/python my_extensions/memory_filter_audit/eval_memory_judge.py --out my_extensions/memory_filter_audit/results/memjudge_$(date +%Y%m%d).json
