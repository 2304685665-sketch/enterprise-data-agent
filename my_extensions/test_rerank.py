"""rerank（LLM 重排）及其接入 internal_doc_search 的离线测试：全部用假 scorer，不调用任何模型/API。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import open_deep_research.internal_doc_search as ids  # noqa: E402
from open_deep_research.rerank import build_rerank_prompt, parse_scores, rerank  # noqa: E402
from test_hybrid_integration import CHUNKS, FakeClient  # noqa: E402


def run(c):
    return asyncio.run(c)


def fixed(text):
    async def s(prompt):
        return text
    return s


# ---------- 纯逻辑 ----------

def test_parse_scores_variants():
    assert parse_scores("[1, 9, 3]", 3) == [1.0, 9.0, 3.0]
    assert parse_scores("分数如下：[1,2]\\n谢谢", 2) == [1.0, 2.0]
    assert parse_scores("[1,2]", 3) is None  # 长度不对
    assert parse_scores("[1,\"a\"]", 2) is None  # 非数字
    assert parse_scores("[true,false]", 2) is None  # bool 不算数字
    assert parse_scores("no json", 1) is None and parse_scores("", 1) is None
    print("✅ test_parse_scores_variants 通过")


def test_rerank_orders_by_score_stable_ties_and_top_n():
    r = run(rerank("q", ["a", "b", "c", "d"], fixed("[5, 9, 5, 1]"), top_n=3))
    assert r.order == [1, 0, 2] and r.fallback_reason is None, r  # 同分 0 在 2 之前
    assert r.scores == [5.0, 9.0, 5.0, 1.0]
    print("✅ test_rerank_orders_by_score_stable_ties_and_top_n 通过")


def test_rerank_fallbacks_keep_original_order():
    async def boom(p):
        raise RuntimeError("x")

    async def slow(p):
        await asyncio.sleep(1)
        return "[1,2]"

    assert run(rerank("q", ["a", "b"], boom, 2)).fallback_reason == "scorer_error:RuntimeError"
    assert run(rerank("q", ["a", "b"], slow, 2, timeout=0.05)).fallback_reason == "scorer_timeout"
    r = run(rerank("q", ["a", "b", "c"], fixed("垃圾"), 2))
    assert r.order == [0, 1] and r.fallback_reason == "unparseable_output" and r.scores is None
    assert run(rerank("q", ["a"], fixed("[1]"), 1)).fallback_reason == "too_few_candidates"
    assert run(rerank("q", [], fixed("[]"), 1)).order == []
    print("✅ test_rerank_fallbacks_keep_original_order 通过")


def test_prompt_contains_query_and_truncates_passages():
    p = build_rerank_prompt("问题X", ["甲" * 2000, "乙"])
    assert "问题X" in p and "候选[0]" in p and "候选[1]" in p
    assert p.count("甲") == 600
    print("✅ test_prompt_contains_query_and_truncates_passages 通过")


# ---------- 接入 internal_doc_search ----------

class RecCollection:
    def __init__(self, log):
        self.log = log

    def count(self):
        return len(CHUNKS)

    def query(self, query_texts, n_results):
        self.log.append(n_results)
        rows = CHUNKS[:n_results]
        return {
            "ids": [[r[0] for r in rows]],
            "documents": [[r[3] for r in rows]],
            "metadatas": [[{"doc_id": r[1], "section": r[2]} for r in rows]],
            "distances": [[r[4] for r in rows]],
        }


class RecClient:
    def __init__(self, log):
        self.log = log

    def get_collection(self, name, embedding_function=None):
        return RecCollection(self.log)


def _search(n, env, scorer=None):
    log = []
    oc, oe = ids.chromadb.PersistentClient, ids.embedding_functions.OpenAIEmbeddingFunction
    ids.chromadb.PersistentClient = lambda path: RecClient(log)
    ids.embedding_functions.OpenAIEmbeddingFunction = lambda **kw: None
    ids._rerank_scorer_override = scorer
    keys = [ids.HYBRID_ENV_VAR, ids.RERANK_ENV_VAR]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        out = run(ids._search_internal_docs("q", n))
    finally:
        ids.chromadb.PersistentClient, ids.embedding_functions.OpenAIEmbeddingFunction = oc, oe
        ids._rerank_scorer_override = None
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    return out, log


def _order(out):
    return [b.split(",")[0].replace("[Source: ", "") for b in out.split("\n\n---\n\n")]


def test_rerank_off_does_not_call_scorer_and_queries_n():
    called = []

    async def spy(p):
        called.append(p)
        return "[1]"

    out, log = _search(3, {}, scorer=spy)
    assert called == [] and log == [3], (called, log)
    print("✅ test_rerank_off_does_not_call_scorer_and_queries_n 通过")


def test_rerank_on_fetches_candidates_reorders_and_keeps_distance():
    out, log = _search(2, {ids.RERANK_ENV_VAR: "1"}, scorer=fixed("[0,0,0,9,0]"))
    assert log == [10], log  # 候选数 = max(n_results, RERANK_CANDIDATES)
    assert _order(out)[0] == "d4", out
    assert "[Source: d4, section: 融资, relevance_distance: 0.450]" in out
    assert out.count("[Source:") == 2
    print("✅ test_rerank_on_fetches_candidates_reorders_and_keeps_distance 通过")


def test_rerank_on_with_failing_scorer_equals_original_top_n():
    async def boom(p):
        raise RuntimeError("down")

    base, _ = _search(3, {})
    out, _ = _search(3, {ids.RERANK_ENV_VAR: "1"}, scorer=boom)
    assert out == base
    print("✅ test_rerank_on_with_failing_scorer_equals_original_top_n 通过")


def test_rerank_and_hybrid_compose():
    out, _ = _search(2, {ids.RERANK_ENV_VAR: "1", ids.HYBRID_ENV_VAR: "1"}, scorer=fixed("[1,1,1,1,9]"))
    assert out.count("[Source:") == 2 and "relevance_distance:" in out
    print("✅ test_rerank_and_hybrid_compose 通过")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
