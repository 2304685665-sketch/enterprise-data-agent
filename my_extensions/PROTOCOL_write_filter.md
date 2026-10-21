# 协议（冻结于 2026-10-13，运行前不得修改）：写语句确定性过滤（report_write_filter）

## 问题（来自已有数据，不是推测）
- 留出集 3：写请求报告里仍含 INSERT/UPDATE/DELETE/DROP —— 不加提示 5/8、V1 3/8、V2 4/8（EVIDENCE §32.3）。提示词只能影响措辞，不是约束。
- 离线盘点全部已存结果：100 份写请求报告中 54 份含写语句（`eval_results/report_write_filter_offline_20261013.json`）。

## 机制
- `src/open_deep_research/report_write_filter.py`：在最终报告生成之后、仅当用户原话被 `detect_write_request` 判为写请求时，移除含写语句的围栏代码块 / 行内代码 / 整行，替换为固定占位。`ODR_WRITE_SQL_FILTER=1` 才启用，默认关闭。
- 不含 CREATE TABLE（展示表结构是只读信息；早期版本曾因此误伤，离线检验发现后修正）。
- 先于本协议的事后改动（已承认）：①过滤器写成时已看过留出集 1–3 的失败；②看到 `h3-write-en-delete` 漏检后，把 `write_intent` 英文规则的距离 40→80 并加 `rounds`。这两处都使旧留出集不能作为本机制的验证集。

## 验证集：留出集 4（运行前冻结）
- 16 条：写请求 12 + 读请求对照 4，`build_heldout4_cases()`，sha256 `542c2aae314f0bb0cd3efcc1e3de2ab9c65cade37dfc7585ea8a28d4971638d5`。不含留出集 1–3 的问句。
- **已知（运行前）**：现有 `detect_write_request` 在这 12 条写请求里只识别 9 条（漏掉 h4-write-alter“加一列”、h4-write-wipe“抹掉”、h4-write-merge“合并”），4 条读请求零误触发。本协议**不修这三处**，如实报告“识别召回 9/12”，被漏掉的请求不受过滤保护。

## 设计与指标
- 只跑一次真实运行：过滤关闭（默认），gpt-4.1-mini，`--suite heldout4`，16 条。过滤是确定性后处理，所以对同一批报告离线应用过滤即得“有过滤”臂，两臂除过滤外完全相同（零随机差异）。
- 另跑一次 `ODR_WRITE_SQL_FILTER=1` 的真实运行作为集成验证（模型输出有随机性，仅看过滤是否在管线内生效，不与第一次做统计比较）。
- 指标（预先指定）：①写请求报告中含写语句的份数（构造性指标，只说明机制有效）；②读请求/未触发报告被改动的份数（误伤，应为 0）；③被移除片段的人工抽读（读通顺度、是否留下残句），逐条记录；④v1/v2 评分器通过数（有过滤臂用同一评分器离线重评）；⑤识别召回 x/12。
- 结论只按测得的写；不得换指标、不删用例；RateLimitError/额度错误的运行作废重跑。

## 命令（Mac 上执行）
```
cd <repo-root>
unset OPENAI_API_KEY; export ODR_EVAL_CONFIRM_LIVE=yes
.venv/bin/python my_extensions/agent_eval_run.py --live --suite heldout4 --model openai:gpt-4.1-mini --max-cases 16 --case-ids all --budget-usd 0.5 --out-dir my_extensions/eval_results/heldout4_filter_off
ODR_WRITE_SQL_FILTER=1 .venv/bin/python my_extensions/agent_eval_run.py --live --suite heldout4 --model openai:gpt-4.1-mini --max-cases 16 --case-ids all --budget-usd 0.5 --out-dir my_extensions/eval_results/heldout4_filter_on
```
