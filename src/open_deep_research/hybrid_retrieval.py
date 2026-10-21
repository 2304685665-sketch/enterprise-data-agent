"""hybrid_retrieval.py —— 向量检索 + BM25 的混合检索排序（RRF 融合）。

纯函数 / 纯 Python 实现，不发任何网络请求、不调用任何模型 API、不新增依赖：
- 中文没有分词器依赖（不引入 jieba），用"连续汉字的二元组（bigram）"做词项，
  英文/数字按 [a-z0-9]+ 切词并小写；这是刻意选的简单且可解释的方案，
  不是对中文分词质量的声称。
- BM25 用 Okapi 公式（k1=1.5, b=0.75），idf 取非负变体
  ln(1 + (N - n + 0.5) / (n + 0.5))，避免词项出现在过半文档里时 idf 变负。
- RRF（Reciprocal Rank Fusion）：score(d) = Σ 1 / (k + rank_i(d))，k 默认 60，
  rank 从 1 开始；只用排名不用原始分数，所以向量距离和 BM25 分数量纲不同
  也不需要归一化。

本模块只负责"给定候选集合、算排序"。从哪里取 chunk 文本、向量排序从哪来
由调用方（internal_doc_search）负责——本文件不接触 Chroma，也不改变工具默认行为。
"""

import math
import re
from typing import Sequence

_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """英文/数字：小写后按 [a-z0-9]+ 切词。
    中文：对每段连续汉字取相邻二元组；只有 1 个汉字的片段保留单字。
    其余字符（标点、空白、下划线等）作为分隔符丢弃。"""
    text = text.lower()
    tokens: list[str] = []
    # 先取英文数字词（汉字不会匹配 [a-z0-9]）
    tokens.extend(_WORD_RE.findall(text))
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25Index:
    """对一组文档文本建立 BM25 索引；scores(query) 返回与文档一一对应的分数。"""

    def __init__(self, docs: Sequence[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._doc_tokens = [tokenize(d) for d in docs]
        self._n = len(self._doc_tokens)
        self._doc_len = [len(t) for t in self._doc_tokens]
        self._avg_len = (sum(self._doc_len) / self._n) if self._n else 0.0
        self._tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for toks in self._doc_tokens:
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self._tf.append(tf)
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self._df = df

    def _idf(self, term: str) -> float:
        n = self._df.get(term, 0)
        return math.log(1.0 + (self._n - n + 0.5) / (n + 0.5))

    def scores(self, query: str) -> list[float]:
        q_terms = tokenize(query)
        out = [0.0] * self._n
        if not q_terms or not self._n:
            return out
        for term in set(q_terms):
            if term not in self._df:
                continue
            idf = self._idf(term)
            for i, tf in enumerate(self._tf):
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self._doc_len[i] / (self._avg_len or 1.0))
                out[i] += idf * f * (self.k1 + 1) / denom
        return out

    def rank(self, query: str, top_n: int | None = None) -> list[int]:
        """返回文档下标，按分数降序；分数为 0（与查询没有任何词项重叠）的文档
        不进入排名；同分按下标升序，保证确定性。"""
        sc = self.scores(query)
        order = sorted((i for i, s in enumerate(sc) if s > 0), key=lambda i: (-sc[i], i))
        return order[:top_n] if top_n is not None else order


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = 60) -> list[tuple[str, float]]:
    """rankings：若干个按相关性降序排好的 id 列表（每个列表内 id 不应重复）。
    返回 [(id, rrf_score)]，分数降序；同分按"首次出现的位置"（先按列表顺序、
    再按列表内顺序）打破平局，保证确定性。"""
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    counter = 0
    for ranking in rankings:
        seen_in_list: set[str] = set()
        for rank, item in enumerate(ranking, start=1):
            if item in seen_in_list:
                continue
            seen_in_list.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in first_seen:
                first_seen[item] = counter
                counter += 1
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))


def hybrid_rank(
    query: str,
    chunk_ids: Sequence[str],
    chunk_texts: Sequence[str],
    vector_ranked_ids: Sequence[str],
    top_n: int,
    rrf_k: int = 60,
) -> list[str]:
    """融合向量排名与 BM25 排名，返回前 top_n 个 chunk id。
    - chunk_ids / chunk_texts：BM25 的候选全集（一一对应）。
    - vector_ranked_ids：向量检索给出的排名（降序），可以是候选全集的子集。
    - BM25 对查询完全没有词项重叠时排名为空，此时结果等于向量排名（不丢向量结果）。"""
    if len(chunk_ids) != len(chunk_texts):
        raise ValueError("chunk_ids and chunk_texts must have the same length")
    bm25 = BM25Index(chunk_texts)
    bm25_ranked_ids = [chunk_ids[i] for i in bm25.rank(query)]
    fused = reciprocal_rank_fusion([list(vector_ranked_ids), bm25_ranked_ids], k=rrf_k)
    return [cid for cid, _ in fused][:top_n]
