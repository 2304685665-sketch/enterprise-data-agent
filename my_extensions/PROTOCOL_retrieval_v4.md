# 协议（冻结于 2026-10-13，运行前）：检索确认性实验 v4

## 问题
§34：给 chunk 加“文档标题头”后，新查询（38 条）上向量检索 Hit@1 0.711→0.868，配对 +7/−1，精确符号检验 p=0.070，未达显著；held-out 上向量检索反而 0.375→0.000。样本太少，结论是“方向偏正，不能下结论”。

## 做法（只补样本，不改任何东西）
- 知识库、embedding 模型、流水线、标题头的写法与 §34 完全相同，**不调参**。
- 新增 60 条冻结查询 `eval_retrieval_queries_v4_large.json`（sha256 `f3d5dfa730e880fab8b22998d735822b134d8cfc06c6c9e8bed239876dce221c`；entity 20 / semantic 22 / keyword 10 / no_answer 8；答案可查的 52 条）。类型比例沿用 v3。所有标注由测试校验目标段落真实存在，且与旧查询无重复。
- 披露：写这批查询时作者已经看过 v3 的结果摘要（标题头对向量检索偏正），因此 v4 的作用是**确认**，不能当作独立于预期的盲测。
- 不跑 LLM 重排：它需要另一套调用 LLM 的评测，当前额度不允许，且不属于“补样本”。

## 判据（运行前约定）
- 主：`new_v4` 单独，对 bm25 / vector / hybrid 三种模式各做标题头索引 vs 原索引的 Hit@1 配对精确符号检验（`retrieval_v4_analysis.py`）。
- 只有在 `new_v4` 单独上 p<0.05 且方向为标题头更好，才写“标题头显著提升了该模式的 Hit@1，并在新批查询上得到确认”。三个模式是三次检验，Bonferroni 后阈值为 0.0167；达到 0.05 但未达 0.0167 只写“未经多重比较校正的提示性结果”。
- 补充：`new_v3 + new_v4` 合并（共 90 条中答案可查约 90−14=76 条）同样做符号检验，只作补充，不能替代主判据。
- 若 `new_v4` 上方向相反或无差异，如实写“§34 的偏正方向没有得到确认”，并把 §34 的措辞降级。
- 单次运行、合成知识库（46 块）、作者自写查询；差异多半不显著，不外推到真实语料。

## 命令（Mac；约 $0.0003）
```
cd <repo-root>
unset OPENAI_API_KEY; export EVAL_RETRIEVAL_ALLOW_API=1
.venv/bin/python my_extensions/eval_retrieval_v4.py --allow-api --out my_extensions/eval_results/retrieval_v4_20261013.json
```
