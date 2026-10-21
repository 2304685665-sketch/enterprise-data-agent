---
name: ledger-entity-arithmetic
description: 内部台账问答中的三条硬规则：实体不存在、账期含义与日期基准、计算必须在 SQL 里完成
triggers: 内部台账, 台账, 账期, 逾期, 占比, 百分比, 客户, 供应商, 删除, 修改, 改成
---
1. 用户点名的客户/供应商如果在 counterparties 里查不到（先 SELECT name FROM counterparties 核对），就回答"台账中不存在该客户/供应商"。不要改查别的编号，不要把"不存在"写成 0 元、0 笔或 0 吨，也不要列出别的实体的数据。
2. "账期为 N 天"指 counterparties.credit_days = N 的客户，是客户自身的属性，不是"逾期了 N 天"。"逾期"的天数另算：meta.as_of 减 due_date。
3. "今天/当前/截至"一律用 meta 表里的 as_of，不要用 date('now') 或 julianday('now')。
4. 百分比、占比、平均、合计都必须在 SQL 里用 SUM/AVG/COUNT 算出来再引用结果（占比用 SUM(部分)/SUM(全部)），不要自己心算、估算或"人工计算"。
5. 用户要求修改或删除台账数据时：台账是只读的，直接说明无法修改、数据未变，并简要给出需要有写权限的人执行的操作；不要联网搜索，不要写通用的操作教程。
