# Prompt 缓存实验协议（运行前冻结，2026-10-13）
**问题**：研究 Agent 的 ReAct 循环每轮都把"系统提示 + 工具描述 + 累积消息"整体重发。OpenAI 的自动前缀缓存（输入≥1024 token 时对相同前缀生效）能否降低这部分输入成本？
**设计**（`prompt_cache_experiment.py`）：模型 gpt-4.1-mini；前缀 = 真实 `research_system_prompt`（代入固定日期、空 mcp_prompt）+ 真实 `build_sql_executor_description(schema_hint=True)` 文本（作为 system 消息的一部分）+ 固定的合成参考材料段（补足体量，使前缀超过 1024 token，如实标注为合成）。5 个固定问题 × 每个 6 轮（每轮在上一轮消息后追加一对"助手/观察"合成消息，模拟 ReAct 累积），共 30 次调用/臂，max_tokens=16。
- 臂 CACHED：前缀逐字相同。
- 臂 CONTROL：system 消息开头加每次调用唯一的随机串，使前缀无法命中缓存（其余相同）。
**主指标**：CACHED 臂在第 2–6 轮的 cached_tokens / prompt_tokens（`usage.prompt_tokens_details.cached_tokens`）；CONTROL 臂应≈0。次要：按官方价格折算的输入成本差（gpt-4.1-mini 输入 $0.40/M，缓存输入 $0.10/M）。
**预先声明**：(1) 结果无论正负都如实写入 EVIDENCE；(2) 若 CACHED 臂命中率≈0，写"未观察到缓存收益"，并检查前缀是否真的≥1024 token；(3) 这测的是缓存机制对真实提示前缀的作用，**不是**端到端研究任务的成本（消息为合成，工具输出不是真实检索结果）；(4) 花费上限 $0.20，脚本内累计超限即停止；(5) 脚本不打印密钥，输出文件已存在则拒绝覆盖。
