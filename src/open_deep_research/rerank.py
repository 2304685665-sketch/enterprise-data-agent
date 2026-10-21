"""可选的 LLM 重排（Rerank）：对检索候选按"与问题的相关度"重新打分排序。

设计要点：
- 纯逻辑与模型调用分离：`rerank()` 接收一个注入的异步 scorer（返回模型原始文本），
  因此测试可以用假 scorer，完全不调用真实模型。
- 失败即降级：scorer 抛异常、超时、输出无法解析、分数个数不对，一律返回原顺序并标记
  `fallback_reason`，绝不让重排故障影响检索本身。
- 只改变顺序，不改变候选内容与向量距离，因此 "[Source: ..., relevance_distance: ...]"
  头行格式与 T3b 的 doc_id 核验不受影响。
- 默认关闭（INTERNAL_DOC_RERANK=1 才启用）；启用会多一次 LLM 调用（有成本和延迟）。
- 局限：LLM 打分有随机性，且只看每个候选的前 RERANK_PASSAGE_CHARS 个字符。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence

RERANK_PASSAGE_CHARS = 600
RERANK_TIMEOUT_SECONDS = 20

Scorer = Callable[[str], Awaitable[str]]  # 输入 prompt，返回模型原始文本


@dataclass
class RerankResult:
    order: list[int]  # 候选下标，按重排后的顺序（已截断到 top_n）
    scores: Optional[list[float]]  # 与输入候选等长的分数；降级时为 None
    fallback_reason: Optional[str] = None


def build_rerank_prompt(query: str, passages: Sequence[str]) -> str:
    lines = [
        "你是检索结果重排器。请判断每个候选片段对回答问题的相关程度，给 0-10 的整数分"
        "（10=直接回答问题，0=无关）。只输出一个 JSON 数组，长度必须等于候选数，"
        "按候选编号顺序给分，不要输出其他文字。",
        f"问题：{query}",
    ]
    for i, p in enumerate(passages):
        lines.append(f"候选[{i}]：{p[:RERANK_PASSAGE_CHARS]}")
    return "\n".join(lines)


def parse_scores(text: str, n: int) -> Optional[list[float]]:
    """从模型输出中解析出长度为 n 的数字数组；解析不了返回 None。"""
    m = re.search(r"\[[^\[\]]*\]", text or "", flags=re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(arr, list) or len(arr) != n:
        return None
    out = []
    for v in arr:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        out.append(float(v))
    return out


async def rerank(
    query: str,
    passages: Sequence[str],
    scorer: Scorer,
    top_n: int,
    timeout: float = RERANK_TIMEOUT_SECONDS,
) -> RerankResult:
    n = len(passages)
    original = list(range(n))[:top_n]
    if n <= 1:
        return RerankResult(original, None, "too_few_candidates" if n else "no_candidates")
    try:
        raw = await asyncio.wait_for(scorer(build_rerank_prompt(query, passages)), timeout=timeout)
    except asyncio.TimeoutError:
        return RerankResult(original, None, "scorer_timeout")
    except Exception as e:  # noqa: BLE001 - 任何模型/网络故障都降级
        return RerankResult(original, None, f"scorer_error:{type(e).__name__}")
    scores = parse_scores(raw, n)
    if scores is None:
        return RerankResult(original, None, "unparseable_output")
    # 稳定排序：同分保持原（检索）顺序，保证确定性
    order = sorted(range(n), key=lambda i: (-scores[i], i))[:top_n]
    return RerankResult(order, scores, None)


def make_llm_scorer(model_name: str) -> Scorer:
    """用 LangChain 的 init_chat_model 构造真实 scorer（测试不使用）。"""
    from langchain.chat_models import init_chat_model

    model = init_chat_model(model=model_name, temperature=0)

    async def _score(prompt: str) -> str:
        resp = await model.ainvoke([{"role": "user", "content": prompt}])
        return resp.content if isinstance(resp.content, str) else str(resp.content)

    return _score
