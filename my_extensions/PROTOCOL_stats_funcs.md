# 统计函数留出实验协议（运行前冻结，2026-10-13）
**问题**：D4 配置下 `L-stats-pstdev`（main2）失败，根因（trace 证据）：SQLite 无 `STDDEV` → 模型报错后把 SQL 预览里的数字手抄进 `python_executor` 计算，结果 29,826,212 vs 真值 29,629,338。机制：`ODR_SQL_STATS_FUNCS=1` 在只读连接上注册 `STDDEV_POP/STDDEV_SAMP/STDDEV/VAR_POP/VAR_SAMP/MEDIAN`，在数据库内对全部行计算，并在工具说明里告知模型。
**为避免只修好开发那一题**：评测用 **新写的 12 题**（`agent_eval_cases_stats.py`，suite `stats`，库 `eval_gen.db` 订单库，用例集 sha256 `8c069a89b5078af4410859aba78c9db9afc72692b1b52984783058975ee67e52`，写于看到任何 S- 运行结果之前）。覆盖总体/样本标准差、中位数，过滤条件含状态/地区/品类/年份/数量。期望值由 Python `statistics` 现算。
**臂**：S0 = D4 配置（`ODR_DIRECT_RESEARCH=1 ODR_SQL_SCHEMA_HINT=1 ODR_SKIP_COMPRESS=1 ODR_SKIP_BRIEF=1`）；S1 = S0 + `ODR_SQL_STATS_FUNCS=1`。模型 gpt-4.1-mini，评分器 v1（预注册）为主，v3 仅作补充。
**主指标**：v1 通过题数，S1 对 S0，配对精确 McNemar。次要：模型调用数/例、python_executor 调用次数、SQL 报错 `no such function` 次数。
**预先声明**：(1) 结果无论正负都写入 EVIDENCE；(2) 12 题样本小，显著性阈值 p<0.05 未必能达到，若只有方向性则如实写"方向性"；(3) 不会在看到结果后改题、改评分器或增删题；(4) 花费上限每臂 $0.25，运行器内置预算守卫；(5) 若任一运行出现模型调用错误/额度耗尽，该运行作废重跑。
