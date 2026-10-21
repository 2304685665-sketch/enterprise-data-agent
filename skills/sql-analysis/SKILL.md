---
name: sql-analysis
description: 用 sql_executor 查询内部数据库并做统计分析的标准流程
triggers: 数据库, 融资, 记录, 统计, 平均, 标准差, 金额, database, funding, average
---
1. 先查结构：`SELECT name, sql FROM sqlite_master WHERE type='table'`，不要凭空猜表名和列名。
2. 每次只发一条 SELECT。PRAGMA、WITH/CTE、任何写操作都会被拒绝；需要多步逻辑时用子查询或分多次查询。
3. 结果最多返回有限行；需要汇总时让 SQL 做聚合（COUNT/SUM/AVG/GROUP BY），不要把大量明细拉回来再手算。
4. 查询报错时仔细阅读原始错误信息再改写下一条查询，不要重复发送同一条失败的语句。
5. SQL 难以表达的计算（标准差、分位数等）：先用 SQL 取回必要数据，再用 python_executor 计算。
6. 结论里写明数据来自哪张表、做了什么过滤；查不到就如实说查不到，不要补一个看起来合理的数字。
