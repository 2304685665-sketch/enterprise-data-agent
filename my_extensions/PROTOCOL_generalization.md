# 协议（冻结于 2026-10-13 23:2x，任何 gen 运行之前）：换领域泛化检验

## 问题
schema 提示（§33.1）和直连模式（§39）是在 companies / funding_rounds 的库上设计并测量的，其中 schema 提示的动机还来自对该库基线运行轨迹的观察。
这些改进在**完全不同的领域和表结构**上还成立吗？还是只适配了那一个库？

## 数据与用例（已冻结，哈希写在 `test_agent_eval_cases_gen.py` 里并由测试强制）
- 库 `eval_gen.db`（`build_gen_db.py`，种子 20261014）：customers / products / orders 三张表，表名、列名、取值与主库完全不同；25 位客户、12 款商品、79 笔订单；sha256 `e5c84ff4…675d5`。
- 用例 `agent_eval_cases_gen.py`：40 例（sql_aggregate 8、sql_filter 12、sql_compare 6、zero_result 3、python_stats 3、no_answer 4、safety_write 4），sha256 `e58cd4fb386b41c51b5111c81ae52c6c66af28a2f183625363353e4c2636664f`。期望值全部由 SQL 现算；两道答案并列的实体题（最近下单的客户、订单笔数最多的客户）在写成时被**删除而不是换说法**。
- 用例在看到任何运行结果之前写定；评分器沿用 v1（预先登记），v2 仅作事后补充，`G-` 前缀的金额取自 orders。

## 臂（同一模型 gpt-4.1-mini、Configuration 默认限额，各一次运行）
- G0：全部默认（基线）。
- G1：`ODR_SQL_SCHEMA_HINT=1`。
- G2：`ODR_DIRECT_RESEARCH=1` + `ODR_SQL_SCHEMA_HINT=1`（即 §39/§40 里的 D2 配置）。
- 条件臂 G3（事先声明的规则）：只有当 `fast_D4` 的质量守门（PROTOCOL_fast_path.md）通过时，才追加 G3 = D4 配置；否则不跑。不会因为 G 系列结果好坏而改变这个规则。

## 指标与检验（预先指定）
- 主要：每例模型调用数、每例 SQL 调用数；G1、G2 各自对 G0 做配对 Wilcoxon（`pair_compare.py`），报告均值和相对变化。
- 质量守门：处理臂 v1 通过数不比 G0 低超过 2 条，且 McNemar 精确检验对“通过变失败”的 p>0.05；不满足则写成效率与质量的权衡。
- 同时报告：成本/例、含无依据数字的用例数、按类别拆分、失败 id。

## 解释规则（运行前约定）
- 若 G1 的 SQL 调用相对 G0 下降且显著，写“在换领域的库上也成立”，并限定为 40 例、一次运行、合成数据。
- 若不成立（不下降或质量下降），如实写“该改进没有泛化到新库”，并把 §33.1 的表述降级为“在主库上成立”。不改用例、不改评分器、不重跑到出现好结果。
- 运行中出现 RateLimitError/额度错误的臂作废，换目录重跑，且在 EVIDENCE 里记录。
- 单次运行，同配置运行间波动未测；40 例功效有限，通过数差异多半不显著，不做“质量提升”的表述。

## 命令（Mac 上执行；预算各 $0.6，预计各约 $0.35）
```
bash my_extensions/run_gen_evals.sh
```
