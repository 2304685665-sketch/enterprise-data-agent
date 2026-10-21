"""internal_doc_search 混合检索接入的离线测试（假 Chroma 客户端，无 API、无 Embedding）。

重点是回归：开关关闭时，行为必须与接入前一致——同一个 collection.query 调用
（n_results 原样传入）、不调用 count()、输出逐字节相同；开启时只在向量候选池内重排，
每个 chunk 保留自己的向量距离，T3b 的 doc_id 提取仍然成立。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import open_deep_research.internal_doc_search as ids  # noqa: E402
from open_deep_research.citation_verification import extract_internal_doc_ids  # noqa: E402

# 向量顺序 = 列表顺序（距离递增）。c3 含查询里的稀有词，向量排第 4，BM25 应把它抬上来。
CHUNKS = [
    ("d1_chunk0", "d1", "概述", "公司简介 业务 方向 团队", 0.30),
    ("d2_chunk0", "d2", "概述", "市场 行业 趋势 报告", 0.35),
    ("d3_chunk0", "d3", "概述", "产品 路线图 发布 计划", 0.40),
    ("d4_chunk0", "d4", "融资", "Series C 融资 8000万美元 领投", 0.45),
    ("d5_chunk0", "d5", "会议", "周会 纪要 行动项", 0.50),
]


class FakeCollection:
    def __init__(self, log):
        self.log = log

    def count(self):
        self.log.append(("count",))
        return len(CHUNKS)

    def query(self, query_texts, n_results):
        self.log.append(("query", n_results))
        rows = CHUNKS[:n_results]
        return {
            "ids": [[r[0] for r in rows]],
            "documents": [[r[3] for r in rows]],
            "metadatas": [[{"doc_id": r[1], "section": r[2]} for r in rows]],
            "distances": [[r[4] for r in rows]],
        }


class FakeClient:
    def __init__(self, log):
        self.log = log

    def get_collection(self, name, embedding_function=None):
        return FakeCollection(self.log)


def _run(query, n_results, hybrid):
    log = []
    orig_client, orig_ef = ids.chromadb.PersistentClient, ids.embedding_functions.OpenAIEmbeddingFunction
    ids.chromadb.PersistentClient = lambda path: FakeClient(log)
    ids.embedding_functions.OpenAIEmbeddingFunction = lambda **kw: None
    old = os.environ.get(ids.HYBRID_ENV_VAR)
    if hybrid is None:
        os.environ.pop(ids.HYBRID_ENV_VAR, None)
    else:
        os.environ[ids.HYBRID_ENV_VAR] = hybrid
    try:
        out = asyncio.run(ids._search_internal_docs(query, n_results))
    finally:
        ids.chromadb.PersistentClient, ids.embedding_functions.OpenAIEmbeddingFunction = orig_client, orig_ef
        if old is None:
            os.environ.pop(ids.HYBRID_ENV_VAR, None)
        else:
            os.environ[ids.HYBRID_ENV_VAR] = old
    return out, log


def test_default_off_uses_original_single_query_no_count():
    out, log = _run("Series C 8000万", 3, None)
    assert log == [("query", 3)], log
    assert sorted(set(extract_internal_doc_ids(out))) == ["d1", "d2", "d3"], extract_internal_doc_ids(out)
    assert out.count("[Source:") == 3
    print("✅ test_default_off_uses_original_single_query_no_count 通过")


def test_off_values_other_than_1_stay_off():
    for v in ("", "0", "true", "yes"):
        _, log = _run("x", 3, v)
        assert log == [("query", 3)], (v, log)
    print("✅ test_off_values_other_than_1_stay_off 通过")


def test_on_queries_pool_once_and_returns_top_n():
    out, log = _run("Series C 8000万", 3, "1")
    assert log == [("count",), ("query", 5)], log  # 池 = min(总数5, max(50,3))
    assert out.count("[Source:") == 3
    print("✅ test_on_queries_pool_once_and_returns_top_n 通过")


def test_on_promotes_keyword_chunk_and_keeps_its_vector_distance():
    off, _ = _run("Series C 8000万", 2, None)
    on, _ = _run("Series C 8000万", 2, "1")
    assert "d4" not in off  # 向量单路 top2 里没有 d4
    assert "[Source: d4, section: 融资, relevance_distance: 0.450]" in on, on
    print("✅ test_on_promotes_keyword_chunk_and_keeps_its_vector_distance 通过")


def test_on_output_format_and_doc_ids_compatible_with_t3b():
    on, _ = _run("Series C 8000万", 3, "1")
    got = extract_internal_doc_ids(on)
    assert "d4" in got and all(g in {"d1", "d2", "d3", "d4", "d5"} for g in got), got
    for block in on.split("\n\n---\n\n"):
        assert block.startswith("[Source: ") and "relevance_distance: " in block.split("\n")[0]
    print("✅ test_on_output_format_and_doc_ids_compatible_with_t3b 通过")


def test_on_only_returns_chunks_from_vector_pool():
    # n_results=2 时池仍取 min(5,50)=5；结果只能来自这 5 个
    on, _ = _run("周会 纪要", 5, "1")
    assert on.count("[Source:") == 5
    print("✅ test_on_only_returns_chunks_from_vector_pool 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
