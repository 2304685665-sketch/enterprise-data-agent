"""internal_doc_search 工具 —— 检索企业内部非结构化文档（RAG）。

设计理念：跟 sql_executor 的"不做RAG"刚好相反——sql_executor 面对的是结构化的
schema，信息密度高，模型自己探索就够了；这里面对的是非结构化文档（公司介绍、
会议纪要、行业报告），需要语义检索才能找到相关片段，这是完全不同的场景，
两者不矛盾（详见 IMPROVEMENTS.md 的角色划分记录）。

已知限制（经过三轮对照实验确认）：
- 使用 OpenAI text-embedding-3-small，每次查询产生少量 API 费用
- 只在小型模拟知识库（10篇文档）上验证过基本检索能力，未做大规模评测
- 无答案场景的拒答能力尚未系统性验证
"""

import asyncio
import os
import chromadb
from open_deep_research import faiss_store
from dotenv import load_dotenv
load_dotenv()
from chromadb.utils import embedding_functions
from langchain_core.tools import tool

from open_deep_research import doc_content_guard
from open_deep_research.hybrid_retrieval import hybrid_rank
from open_deep_research.rerank import make_llm_scorer, rerank
from open_deep_research.tool_reliability import (
    TRANSIENT_NETWORK_EXCEPTION_TYPES,
    TransientToolError,
    NonRetryableInfrastructureError,
)

CHROMA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "my_extensions", "chroma_db"
)

# 2026-10 加入：之前这个工具完全没有超时保护——Chroma 查询卡住时没有任何一层
# 会打断它。15s 是刻意选的"够一次正常 Embedding API 调用用，但不会让一次卡住
# 的调用无限期占着"的折衷值，和 sql_executor 的 5s、python_executor 的 15s
# 属于同一量级，不是精确调优过的数字。
INTERNAL_DOC_SEARCH_TIMEOUT_SECONDS = 15

# 混合检索（向量候选池 + BM25 重排，RRF 融合）——默认关闭，环境变量
# INTERNAL_DOC_HYBRID_SEARCH=1 才启用。关闭时检索路径与启用前完全一致（同一个
# collection.query 调用、不额外调用 count()/Embedding）。
#
# 设计取舍（必须随代码一起理解）：BM25 只在"向量检索取回的候选池"里重排，候选池上限
# HYBRID_CANDIDATE_POOL_MAX 个 chunk（且不超过 collection 总数）。这不是完整的双路
# 召回：纯 BM25 能命中、但没进向量候选池的 chunk 无法参与。这样做是为了让每个返回的
# chunk 都有真实的向量距离，保持 "[Source: ..., relevance_distance: ...]" 头行格式不变，
# 因而 T3b 的 doc_id 提取与引用核验无需改动。要做真正的双路召回，需要重新设计候选
# 结果的数据结构和距离字段，而不是给 BM25-only 的结果伪造一个距离。
HYBRID_ENV_VAR = "INTERNAL_DOC_HYBRID_SEARCH"
HYBRID_CANDIDATE_POOL_MAX = 50


# LLM 重排——默认关闭，INTERNAL_DOC_RERANK=1 启用（会多一次 LLM 调用）。
# 启用时先取 RERANK_CANDIDATES 个候选（混合检索开启则是混合排序后的前 N 个），
# 重排后截取 n_results。重排失败自动降级为原顺序。模型由 INTERNAL_DOC_RERANK_MODEL 指定。
RERANK_ENV_VAR = "INTERNAL_DOC_RERANK"
RERANK_MODEL_ENV_VAR = "INTERNAL_DOC_RERANK_MODEL"
RERANK_CANDIDATES = 10
_rerank_scorer_override = None  # 测试注入假 scorer 用


def _rerank_enabled() -> bool:
    return os.environ.get(RERANK_ENV_VAR, "").strip() == "1"


def _get_rerank_scorer():
    if _rerank_scorer_override is not None:
        return _rerank_scorer_override
    model = os.environ.get(RERANK_MODEL_ENV_VAR, "").strip()
    if not model:
        from open_deep_research.configuration import Configuration

        model = Configuration().summarization_model
    return make_llm_scorer(model)


def _reorder(results: dict, idx: list[int]) -> dict:
    out = dict(results)
    for key in ("ids", "documents", "metadatas", "distances"):
        if out.get(key) is not None:
            out[key] = [[results[key][0][i] for i in idx]]
    return out


def _hybrid_enabled() -> bool:
    return os.environ.get(HYBRID_ENV_VAR, "").strip() == "1"


def _apply_hybrid(results: dict, query: str, n_results: int) -> dict:
    """对一次向量查询结果（候选池）做 BM25+RRF 重排，取前 n_results 个，保持原有字典结构，
    每个 chunk 保留自己的向量距离。不引入候选池之外的 chunk。"""
    ids = results["ids"][0]
    if not ids:
        return results
    docs = results["documents"][0]
    order = hybrid_rank(query, ids, docs, vector_ranked_ids=ids, top_n=n_results)
    pos = {cid: i for i, cid in enumerate(ids)}
    idx = [pos[cid] for cid in order]
    out = dict(results)
    for key in ("ids", "documents", "metadatas", "distances"):
        if out.get(key) is not None:
            out[key] = [[results[key][0][i] for i in idx]]
    return out

INTERNAL_DOC_SEARCH_DESCRIPTION = (
    "Search the company's internal document knowledge base (company introductions, "
    "product descriptions, industry reports, meeting notes, investment memos). "
    "Use this INSTEAD of web search when the question is about internal company "
    "opinions, internal analysis, or company/product background that would not be "
    "publicly available online. Returns relevant text snippets with their source "
    "document. If no clearly relevant results are found, say so honestly rather "
    "than guessing."
)


async def _search_internal_docs(query: str, n_results: int) -> str:
    """核心实现：执行检索并格式化结果。

    网络层异常（openai 连接/超时类）不在这里被吞掉转成字符串，而是转换成
    TransientToolError 向上抛——是否转换回字符串由下面两个调用入口之一决定：
    公开的 internal_doc_search 工具会转换（保留原有的直接调用行为），
    execute_tool_safely 用的 internal_doc_search_tool_call 不转换（让重试
    判断能看到真实异常类型，而不是解析字符串前缀）。
    """
    rerank_on = _rerank_enabled()
    fetch_n = max(n_results, RERANK_CANDIDATES) if rerank_on else n_results

    def _run_search():
        if faiss_store.backend() == "faiss":      # ODR_VECTOR_BACKEND=faiss：可替换向量库后端，其余逻辑不变
            collection = faiss_store.FaissCollection()
        elif faiss_store.backend() == "pgvector":  # ODR_VECTOR_BACKEND=pgvector：PostgreSQL + pgvector
            from open_deep_research import pgvector_store
            collection = pgvector_store.PgVectorCollection()
        else:
            client = chromadb.PersistentClient(path=CHROMA_DIR)
            openai_ef = embedding_functions.OpenAIEmbeddingFunction(
                api_key=os.environ.get("OPENAI_API_KEY"),
                model_name="text-embedding-3-small",
            )
            collection = client.get_collection(
                "company_knowledge_base", embedding_function=openai_ef
            )
        if not _hybrid_enabled():
            return collection.query(query_texts=[query], n_results=fetch_n)
        total = collection.count()
        if total == 0:
            return collection.query(query_texts=[query], n_results=fetch_n)
        pool = min(total, max(HYBRID_CANDIDATE_POOL_MAX, fetch_n))
        return _apply_hybrid(collection.query(query_texts=[query], n_results=pool), query, fetch_n)

    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_run_search), timeout=INTERNAL_DOC_SEARCH_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # 已知局限，诚实记录而非假装已解决：asyncio.wait_for 包一个用
        # asyncio.to_thread 跑的阻塞调用时，超时只会让我们这边提前返回，
        # 后台线程里真正的 Embedding API 请求不会被真正打断，会继续跑到它
        # 自己的网络超时/完成为止，结果只是被丢弃——这是"放弃等待"，不是
        # python_executor 那种用 docker kill 真正终止执行的"确认清理"。
        #
        # 2026-10 分类修正：这不是"这次查询的内容有问题"（tool_input_error），
        # 是网络/Embedding 服务层面的故障，应该归为 infrastructure_error、
        # 计入熔断——但不能重试（见 NonRetryableInfrastructureError 的文档
        # 字符串：被放弃的后台线程仍在运行，重试会让这类线程持续堆积）。
        # 改成 raise 而不是直接 return 字符串，这样 execute_tool_safely 才能
        # 按真实的异常类型分类，不用猜字符串。
        raise NonRetryableInfrastructureError(
            f"Document search error: query did not complete within "
            f"{INTERNAL_DOC_SEARCH_TIMEOUT_SECONDS}s and was abandoned "
            "(the underlying request may still be running in the background)."
        )
    except TRANSIENT_NETWORK_EXCEPTION_TYPES as e:
        raise TransientToolError(f"Document search error: {type(e).__name__}: {str(e)}")

    if not results["ids"][0]:
        return "No relevant documents found in the internal knowledge base."

    if rerank_on:
        try:
            scorer = _get_rerank_scorer()
        except Exception:  # noqa: BLE001 - 构造模型失败（缺 key 等）同样降级为不重排
            scorer = None
        if scorer is None:
            results = _reorder(results, list(range(len(results["ids"][0])))[:n_results])
        else:
            rr = await rerank(query, results["documents"][0], scorer, top_n=n_results)
            results = _reorder(results, rr.order)

    _doc_guard_on = doc_content_guard.enabled()   # ODR_DOC_INJECTION_GUARD=1：检索内容间接注入防护（默认关闭）
    output_lines = []
    for i in range(len(results["ids"][0])):
        doc_id = results["metadatas"][0][i]["doc_id"]
        section = results["metadatas"][0][i]["section"]
        distance = results["distances"][0][i]
        text = results["documents"][0][i]
        if _doc_guard_on:
            text, _ = doc_content_guard.sanitize_chunk(text)
        output_lines.append(
            f"[Source: {doc_id}, section: {section}, relevance_distance: {distance:.3f}]\n{text}"
        )

    body = "\n\n---\n\n".join(output_lines)
    return (doc_content_guard.NOTICE + "\n" + body) if _doc_guard_on else body


@tool(description=INTERNAL_DOC_SEARCH_DESCRIPTION)
async def internal_doc_search(query: str, n_results: int = 3) -> str:
    """Search internal documents and return relevant snippets with sources.

    Args:
        query: Natural language search query.
        n_results: How many top results to return (default 3).

    Returns:
        Formatted text with each result's source document and content,
        or an error message if the search failed.
    """
    try:
        return await _search_internal_docs(query, n_results)
    except (TransientToolError, NonRetryableInfrastructureError) as e:
        return str(e)
    except Exception as e:
        return f"Document search error: {type(e).__name__}: {str(e)}"


async def internal_doc_search_tool_call(query: str, n_results: int = 3) -> str:
    """execute_tool_safely 实际调用的入口，而不是上面的公开工具。

    行为和公开工具完全一致，唯一区别是不吞掉 TransientToolError——
    execute_tool_safely 需要看到真实异常类型才能判断是否值得重试。
    LangChain 通过 `.ainvoke()` 直接调用公开工具时（包括现有测试）走的是
    上面那条会把 TransientToolError 转换回字符串的路径，不受这个函数影响。
    """
    return await _search_internal_docs(query, n_results)
