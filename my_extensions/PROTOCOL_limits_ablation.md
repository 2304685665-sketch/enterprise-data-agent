# 协议（冻结于 2026-10-12，运行前不得修改）：Agent 调用上限约束的受控对比

## 要回答的问题
把研究 Agent 的并行/迭代/工具调用上限从 Configuration 默认值收紧到 `--relaxed-limits` 预设后，同样的 53 条任务上：模型调用数、费用（Token 的代理）、SQL 工具调用数、通过率、失败类型如何变化？

## 这个实验**不是**什么
- 这里的“约束”只是配置里的上限（并行 researcher 5→2、supervisor 迭代 6→3、单 researcher 工具调用 10→10、结构化输出重试 3→1），**不是**“计划范围约束”提示词；本项目没有实现后者，不得在简历里宣称。
- 不是对“过度拆解”的根因实验；根因只能由逐工具追踪（run_trace）单独支持。

## 臂
| 臂 | 配置 | 数据来源 |
|---|---|---|
| A 默认 | 不加限额预设（Configuration 默认） | **新跑** 53 条 |
| B2 收紧 | `--relaxed-limits` | **新跑** 53 条（与 A 同一代码版本） |
| B1 收紧（旧） | 已有 `agent_eval_run_20261012T122849Z` | 已有；与 B2 比较只用来估计运行间波动 |
相同：模型 `openai:gpt-4.1-mini`（四个角色）、`--search-api none`、`--memory off`、同一 53 条用例与评分器（v1 预注册为主，v2 事后重评为辅）、`MEMORY_GUARD` 与 `DEEP_RESEARCH_GROUNDING_GUARD` 均不设置（关闭）。

## 分步与停止规则（预算：累计费用上限 $8，此前已花约 $1.3）
1. 先对 A 做 **5 条试跑**（固定：latest-RoboWorks, cmp-most-total, avg-RoboWorks, absent-company, since2026-sum），`--budget-usd 1.00`（运行前修订：原为 0.60；默认配置每条费用未知，担心封顶过紧导致后续用例被拦、数据不完整；这只改封顶，不改用例、评分或比较方式）。用实测的每条费用外推 53 条总费用。
2. 外推总费用 ≤ $3.0 才继续跑 A 的 53 条（`--budget-usd 3.5`）；否则停止，把结果如实记为“默认配置在预算内跑不完 53 条”，只报告试跑数据。
3. 再跑 B2 的 53 条（`--budget-usd 1.0`）。
4. 任何一次触发预算刹车、`budget_aborted`、密钥扫描命中或数据库哈希变化：该臂标记不完整，不得与完整臂直接比较。

## 预先指定的指标与检验（运行前冻结）
- 主要：每条模型调用数、每条费用（美元，按 `eval_budget` 核算）。配对（同一用例），**Wilcoxon 符号秩检验**，报告均值差、中位数差。
- 次要：SQL 调用数/条；v1 通过数、v2 通过数（配对 **McNemar 精确检验**）；`UNGROUNDED_REPORT_FIGURES` 命中数；失败原因（issues）分布；平均耗时。
- 运行间波动：B1 vs B2 用同样的指标；A vs B2 的差异只有明显大于 B1 vs B2 的差异时，才说是约束造成的。
- 不做事后换指标、不根据结果删除用例；无效运行（run_status ≠ finished）单独列出。

## 命令（在 Mac 上执行，会产生费用；云端沙箱连不上 OpenAI）
```
cd <repo-root>
unset OPENAI_API_KEY            # 清掉 shell 里的假 key，真实 key 由 .env 提供
export ODR_EVAL_CONFIRM_LIVE=yes
# 步骤1：A 的 5 条试跑
.venv/bin/python my_extensions/agent_eval_run.py --live --model openai:gpt-4.1-mini --max-cases 5 \
  --case-ids latest-RoboWorks,cmp-most-total,avg-RoboWorks,absent-company,since2026-sum \
  --budget-usd 1.00 --out-dir my_extensions/eval_results/limits_A_defaults_pilot
# 步骤2（仅当外推 ≤ $3.0）
.venv/bin/python my_extensions/agent_eval_run.py --live --model openai:gpt-4.1-mini --max-cases 53 --case-ids all \
  --budget-usd 3.5 --out-dir my_extensions/eval_results/limits_A_defaults_full
# 步骤3
.venv/bin/python my_extensions/agent_eval_run.py --live --model openai:gpt-4.1-mini --max-cases 53 --case-ids all \
  --relaxed-limits --budget-usd 1.0 --out-dir my_extensions/eval_results/limits_B2_relaxed_full
# 对比
.venv/bin/python my_extensions/agent_eval_compare.py limits_A_defaults_full limits_B2_relaxed_full
.venv/bin/python my_extensions/agent_eval_compare.py agent_eval_run_20261012T122849Z limits_B2_relaxed_full
```
