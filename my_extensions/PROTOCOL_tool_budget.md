# 协议（冻结于 2026-10-13，运行前不得修改）：单个 researcher 工具调用预算对照

## 问题陈述（来自逐工具追踪，不是推测）
- 数据：`limits_A_defaults_full` 的 `trace/*/run_trace.jsonl`（53 条，其中 52 条有追踪）。
- 我们的系统**没有“过度拆解”**：平均 1.02 个 researcher，最多 2 个。
- 工具调用偏多发生在**单个 researcher 内部的重复 SQL 探测**：全部 `tool_start` 中 `sql_executor` 212 次（平均 4.0 次/条）；简单计数 `count-NeuralPath` 用了 6 次；库中没有答案的 `absent-company` 用了 9 次。固定流水线的模型调用中位数为 13 次/条，所以即使 SQL 探测减少，总调用的下降空间有限。
- 因此本实验对应的是“按角色的工具调用预算”这一半，而不是“计划范围约束”（后者对应的过度拆解问题在我们的负载上不存在，不会为了对齐而制造这个问题）。

## 机制与臂
- 机制：把单个 researcher 的最大工具调用轮数 `max_react_tool_calls` 从 10 降到 4（现有配置项，不改代码逻辑；runner 只新增 `--react-calls N` 开关）。一轮 = 一次 researcher_tools 往返，可含多个并行工具调用。
- 臂 A2：Configuration 默认值；臂 C：`--react-calls 4`。**两臂都在当前代码上新跑**（当前代码相对 6418563 多了最终报告提示词规则和写请求识别，所以不复用 `limits_A_defaults_full`）。
- 相同：模型 `openai:gpt-4.1-mini`（四个角色）、`--search-api none`、`--memory off`、53 条用例、评分器 v1（预注册为主）、v2（事后，辅助）。

## 预先指定的指标与检验
- 主要：每条模型调用数、每条 SQL 调用数。配对 Wilcoxon（`limits_ablation_analysis.py`）。
- 质量：v1 通过数、v2 通过数；McNemar 精确检验；含无依据数字的用例数；失败 id 列表。
- 命中预算：C 臂中 SQL 调用达到上限附近（≥4 次）的用例数，用来说明预算是否真的在起作用。
- **“质量不退步”的判据（运行前定）**：C 的 v1 通过数不比 A2 低超过 2 条且 McNemar p>0.05，才可写“未检测到质量下降”；n=53 功效有限，**不能**写成“质量保持不变”或“等效”。
- 效率结论按测得的写：下降、不变或上升都如实记录；不换指标，不删用例；`run_status≠finished` 的用例单独列出，该臂不完整时不做比较。

## 命令（在 Mac 上执行；有 RateLimitError 时该臂作废，换目录重跑）
```
cd <repo-root>
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
.venv/bin/python my_extensions/agent_eval_run.py --live --model openai:gpt-4.1-mini --max-cases 53 --case-ids all --budget-usd 1.5 --out-dir my_extensions/eval_results/budget_A2_defaults
.venv/bin/python my_extensions/agent_eval_run.py --live --model openai:gpt-4.1-mini --max-cases 53 --case-ids all --react-calls 4 --budget-usd 1.5 --out-dir my_extensions/eval_results/budget_C_react4
```

## 修订（2026-10-13 19:05，运行 C 臂之前追加）
- 臂 A2 直接使用已完成的 `main_final`（53 条，Configuration 默认值，当前代码，无限流/无无效运行；$0.5803）。理由：协议要求的是“当前代码上的默认值运行”，`main_final` 正是；自上次提交以来行为相关改动只有默认关闭的 `ODR_SQL_SCHEMA_HINT` 开关与 Dockerfile。这样少花一次 $0.6，但**只有一次 A2 运行，没有重复**，同配置的运行间波动仍未单独测量。
- 只需新跑 C 臂：`--react-calls 4`，输出目录 `budget_C_react4`。
