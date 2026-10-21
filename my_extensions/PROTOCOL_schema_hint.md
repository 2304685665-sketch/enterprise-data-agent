# 实验协议：把实时 schema 放进 sql_executor 工具说明（预先指定，2026-10-13，在任何处理组运行之前写成）

## 动机（来自已有数据，不是为了凑数字）
- EVIDENCE §31/PROTOCOL_tool_budget：本工作负载平均 1.02 个 researcher，不存在“过度拆分”；浪费主要是单个 researcher 内部反复试探 SQL（sql_executor 平均 4.0 次/条）。
- 工具说明和研究员提示词都告诉模型“你不知道 schema，请先查 sqlite_master”，并且模型会猜列名（如 `founding_year`）再靠报错纠正。
- 机制：`ODR_SQL_SCHEMA_HINT=1` 时，从数据库本身实时读出建表语句放进工具说明，并把研究员提示词里“先查 sqlite_master”的指令换成“schema 已在工具说明里”。关闭时与旧行为逐字一致（`test_sql_schema_hint.py`）。

## 设计
- 用例：main2，63 条，sha256 `fd9d26e8…cda5f2f`（eval_large.db，sha256 `6a893fe0…c5432`），模型 gpt-4.1-mini，默认限额。
- 基线 = `main2_final`（schema 注入关闭）；处理组 = `main2_schema`（`ODR_SQL_SCHEMA_HINT=1`，其余完全相同），预算 $1.5。
- 若 main2_final 因限速/额度无效，则两组都重跑；不选择性保留。

## 主要指标与检验（运行前指定）
1. 每条用例的模型调用次数：配对 Wilcoxon（`limits_ablation_analysis.py` 同款实现）；同时报告均值差、中位数差。
2. 每条用例的 sql_executor 调用次数：同上。
3. 质量守门：v1 通过数，McNemar 精确检验；“无质量退化”的判据 = 处理组 v1 通过数不低于基线 2 条以上，且 McNemar p>0.05 或方向为处理组更好。
4. 报告：按类别拆分，不做事后挑选类别。

## 解释规则（运行前约定）
- 调用次数显著下降且质量守门通过 → 如实报告下降幅度，限定在“合成数据、该模型、63 条、单次运行”。
- 下降不显著或质量退化 → 如实写“无显著效果/有退化”，不改处理方式后重跑到出现好结果；若要改机制必须当作新实验、换新用例。
- 单次运行没有重复种子，所以只能说“这一次运行中”，不写成稳定的百分比提升。
