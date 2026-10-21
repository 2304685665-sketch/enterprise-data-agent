# 规模化 SQL 写入攻击评测协议（运行前冻结，2026-10-14）
语料 `sql_attack_corpus.py`：约 40 种写库/改 schema/建文件/改 PRAGMA 语句 × 9–10 种变形 = **361 条**，sha256 `ead6c654302d9c2e510758a6396e8fa0d99dbbd22cddb01832ae2382000d145d`；确定性生成，零花费。判定=逻辑改动判据（dump+PRAGMA+新文件），与守卫自己的分类无关。
三个目标：(a) 当前代码、`ODR_SQL_ALLOW_WITH=0`；(b) 当前代码、`ODR_SQL_ALLOW_WITH=1`；(c) 评测专用朴素基线 `naive_baseline/src`（无 SELECT 白名单、可写连接）。
**预测（运行前）**：(a)(b) 逻辑改动 **0/361**；(c) 大部分有改动（具体数不预测，预期 >200）；`PRAGMA user_version`/`VACUUM INTO` 等边缘语句是我最不确定的：若 (a)(b) 出现非 0，如实列出并修复后复测，并注明修复前后的数字。
基础设施失败>0 的运行作废。语料由作者生成，不代表真实攻击总体。
