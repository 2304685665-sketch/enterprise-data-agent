# 协议（冻结于 2026-10-13，运行前不得修改）：单研究员直连模式（跳过 supervisor）

## 问题（来自已有数据）
- 一个只需 1 条 SQL 的问题（如 `total-RoboWorks`）用了 15 次模型调用、6 次 SQL；主集每例平均 13.3 次模型调用、4.26 次 SQL（`main_final`），大库 13.3 次、4.11 次（`main2_final`）。
- 固定开销来自为“多方向并行网络调研”设计的 supervisor 往返（评测配置 `allow_clarification=False`，所以不是澄清）。我们的负载是单库查询，不需要并行拆解。

## 机制
- `ODR_DIRECT_RESEARCH=1`：`write_research_brief` 之后不进入 supervisor，直接把简报交给一个 researcher，结果以与 `supervisor_tools` 相同的方式写入 `notes/raw_notes/degradation_events/fetched_*`，再进入最终报告。默认关闭。
- 代价与适用范围：失去 supervisor 的并行拆解与迭代复核，**不适合**需要多方向网络调研的任务；这里只在 `--search-api none` 的数据库问答评测上检验。
- 离线测试 `test_direct_research.py`（4 个）：开关默认关、图中有新节点与出口边、状态映射与 supervisor 一致、researcher 失败时降级。

## 臂与用例
- 对照臂 A：已完成的 `main_final`（主集 53 条）与 `main2_final`（大库 63 条），Configuration 默认值，同一模型 gpt-4.1-mini（四个角色）。
- 处理臂 B：`ODR_DIRECT_RESEARCH=1`，其余完全相同，输出 `direct_main`（53）、`direct_main2`（63）。
- 对照臂只有一次运行，同配置运行间波动未单独测量；有 RateLimitError/额度错误的运行作废，换目录重跑。

## 预先指定的指标与检验
- 主要：每条模型调用数、每条 SQL 调用数（配对 Wilcoxon，`pair_compare.py`）；报告平均数与相对变化。
- 质量：v1 通过数、v2 通过数（事后）；McNemar 精确检验；含无依据数字的用例数；失败 id 列表。
- “质量不退步”的判据（运行前定）：B 的 v1 通过数不比 A 低超过 2 条且 McNemar p>0.05，才可写“未检测到质量下降”；n=53/63 功效有限，不写成“质量保持不变”。
- 效率结论按测得的写：下降、不变或上升都如实记录；不换指标，不删用例。若调用数下降但质量下降超过判据，两者并列写出。

## 命令
```
bash my_extensions/run_direct_evals.sh
```
