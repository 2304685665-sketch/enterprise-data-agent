"""内部知识库检索评测：向量 / BM25 / 混合（BM25 + RRF）三种排序的 Hit@1 与 MRR。

重要约束（写在代码里，不只写在文档里）：
- 查询集 my_extensions/eval_retrieval_queries.json 在看到任何检索结果之前写定，
  评测后不得为改善指标而修改查询/标注/参数（RRF k=60、候选池上限 50、
  BM25 k1=1.5 b=0.75 均为预先固定值）。
- 只有 --mode bm25 完全离线（不调用任何 API）。--mode vector / hybrid 需要真实
  的 OpenAI Embedding 调用（每条查询 1 次 text-embedding-3-small），因此必须
  同时传 --allow-api 并设置环境变量 EVAL_RETRIEVAL_ALLOW_API=1，否则直接拒绝。
- 25 个 chunk 的规模下存在天花板效应：指标没有提高不代表实现无价值，指标提高
  也只说明在这组数据上更好，不能外推到生产知识库。
- 无答案类查询不计入 Hit@1/MRR，只做描述性记录（见 no_answer_records，独立于 evaluate）。
- 辅助分析（仅新增，不改变 evaluate 的任何输出）：多目标查询的 Target Coverage@k、
  no_answer 查询的描述性记录。二者都不是拒答能力或答案正确性的度量。

指标定义：
- Hit@1：排名第 1 的 chunk 是否属于该查询的标注目标集合。
- MRR：第一个命中标注目标的名次的倒数（从 1 起算），未命中记 0；对有答案的查询取平均。
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from open_deep_research.hybrid_retrieval import BM25Index, hybrid_rank  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(HERE, "knowledge_base")
QUERIES_PATH = os.path.join(HERE, "eval_retrieval_queries.json")
CANDIDATE_POOL_MAX = 50
RRF_K = 60


def load_kb_chunks(kb_dir: str = KB_DIR) -> list[dict]:
    """与 build_knowledge_base.split_into_chunks 相同的切分规则（按 "## " 标题；
    第一个 "## " 之前的“开头”内容被丢弃）。chunk id 为 {doc_id}_chunk{i}。
    不 import 该脚本：它在 import 时就会构造 OpenAI Embedding 对象。
    真实模式下会与 Chroma 里的 chunk 做一致性核对（见 _check_against_chroma）。"""
    chunks: list[dict] = []
    for fn in sorted(os.listdir(kb_dir)):
        if not fn.endswith(".md") or fn == "ANSWER_KEY.md":
            continue
        doc_id = fn[:-3]
        sections: list[tuple[str, list[str]]] = []
        title, buf = None, []
        for line in open(os.path.join(kb_dir, fn), encoding="utf-8").read().split("\n"):
            if line.startswith("## "):
                if title is not None:
                    sections.append((title, buf))
                title, buf = line.replace("## ", "").strip(), [line]
            else:
                buf.append(line)
        if title is not None:
            sections.append((title, buf))
        i = 0
        for title, buf in sections:
            text = "\n".join(buf).strip()
            if text:
                chunks.append({"id": f"{doc_id}_chunk{i}", "doc_id": doc_id, "section": title, "text": text})
                i += 1
    return chunks


def load_queries(path: str = QUERIES_PATH) -> list[dict]:
    return json.load(open(path, encoding="utf-8"))["queries"]


def target_chunk_ids(query: dict, chunks: list[dict]) -> set[str]:
    wanted = {(d, s) for d, s in query["targets"]}
    return {c["id"] for c in chunks if (c["doc_id"], c["section"]) in wanted}


def first_hit_rank(ranked_ids: list[str], targets: set[str]) -> int | None:
    for i, cid in enumerate(ranked_ids, start=1):
        if cid in targets:
            return i
    return None


def evaluate(rankings: dict[str, list[str]], queries: list[dict], chunks: list[dict]) -> dict:
    """rankings：query id -> 按相关性降序的 chunk id 列表。
    返回总体与分类型的 Hit@1 / MRR（仅有答案的查询），以及逐条明细。"""
    detail = []
    for q in queries:
        if q["type"] == "no_answer":
            continue
        targets = target_chunk_ids(q, chunks)
        ranked = rankings.get(q["id"], [])
        r = first_hit_rank(ranked, targets)
        detail.append({"id": q["id"], "type": q["type"], "first_hit_rank": r,
                       "hit1": r == 1, "rr": (1.0 / r) if r else 0.0, "top1": ranked[:1]})

    def agg(rows):
        n = len(rows)
        return {"n": n,
                "hit1": (sum(1 for x in rows if x["hit1"]) / n) if n else None,
                "mrr": (sum(x["rr"] for x in rows) / n) if n else None}

    by_type: dict[str, dict] = {}
    for t in sorted({d["type"] for d in detail}):
        by_type[t] = agg([d for d in detail if d["type"] == t])
    return {"overall": agg(detail), "by_type": by_type, "detail": detail}


COVERAGE_KS = (1, 2, 3, 5)
# 以下两项是对冻结 v1 查询集的"分析侧注释"，放在代码里而不是 JSON 里，以保证 JSON 不变。
INDEX_COVERAGE_PROBES = {"N3"}  # 答案在未入索引的 doc01 开头段：测索引覆盖，不是语料无答案
KNOWN_DISTRACTORS = {"N4": [("doc01_synthmotion_intro", "融资历程")]}  # 含其他公司融资金额的干扰段


def target_coverage(rankings: dict[str, list[str]], queries: list[dict], chunks: list[dict],
                    ks: tuple[int, ...] = COVERAGE_KS) -> dict:
    """多目标查询（标注目标 chunk 数 >= 2）的 Target Coverage@k：
    前 k 名中出现的不同目标数 / 目标总数。每个目标只计一次（重复出现的 id 不重复计）。
    单目标与 no_answer 查询不参与。与 Hit@1/MRR 的"任一命中"规则互不影响。"""
    rows = []
    for q in queries:
        if q["type"] == "no_answer":
            continue
        targets = target_chunk_ids(q, chunks)
        listed = {tuple(t) for t in q["targets"]}
        if len(targets) != len(listed):
            # 标注目标无法一一映射到索引 chunk：不静默缩小分母，直接报错
            raise ValueError(f"查询 {q['id']} 的标注目标与索引 chunk 不能一一对应："
                             f"标注 {len(listed)} 个，映射到 {len(targets)} 个 chunk")
        if len(targets) < 2:
            continue
        ranked = rankings.get(q["id"], [])
        row = {"id": q["id"], "type": q["type"], "n_targets": len(targets)}
        for k in ks:
            covered = set(ranked[:k]) & targets
            row[f"coverage@{k}"] = len(covered) / len(targets)
        rows.append(row)
    mean = {f"coverage@{k}": (sum(r[f"coverage@{k}"] for r in rows) / len(rows)) if rows else None for k in ks}
    return {"n_queries": len(rows), "mean": mean, "detail": rows}


def no_answer_records(rankings: dict[str, list[str]], queries: list[dict], chunks: list[dict], mode: str,
                      scores: dict[str, dict[str, float]] | None = None) -> list[dict]:
    """no_answer 查询的描述性记录（不进入任何指标）：记录排名第 1 的 chunk 及其分数
    （scores 提供时；仅 bm25 模式提供 BM25 分数，vector/hybrid 不提供，不伪造 RRF 分数，
    此时 top1_score 与 score_source 均为 None）、是否为已知干扰段。
    不判断拒答是否正确，也不代表 Agent 的回答行为。"""
    by_id = {c["id"]: c for c in chunks}
    out = []
    for q in queries:
        if q["type"] != "no_answer":
            continue
        ranked = rankings.get(q["id"], [])
        distractors = set()
        for d, s in KNOWN_DISTRACTORS.get(q["id"], []):
            distractors |= {c["id"] for c in chunks if (c["doc_id"], c["section"]) == (d, s)}
        top = by_id.get(ranked[0]) if ranked else None
        first_d = next((i for i, cid in enumerate(ranked, start=1) if cid in distractors), None)
        out.append({
            "id": q["id"], "query": q["query"], "mode": mode,
            "analysis_type": "index_coverage_probe" if q["id"] in INDEX_COVERAGE_PROBES else "no_answer_probe",
            "top1_chunk_id": top["id"] if top else None,
            "top1_doc_id": top["doc_id"] if top else None,
            "top1_section": top["section"] if top else None,
            "top1_score": (scores or {}).get(q["id"], {}).get(ranked[0]) if ranked else None,
            "score_source": "bm25" if scores is not None else None,  # None = 该模式未提供分数
            "has_known_distractor": bool(distractors),
            "top1_is_known_distractor": bool(ranked) and ranked[0] in distractors,
            "first_distractor_rank": first_d,
            "n_ranked": len(ranked),
        })
    return out


def bm25_rankings(queries: list[dict], chunks: list[dict]) -> dict[str, list[str]]:
    idx = BM25Index([c["text"] for c in chunks])
    return {q["id"]: [chunks[i]["id"] for i in idx.rank(q["query"])] for q in queries}


def bm25_scores(queries: list[dict], chunks: list[dict]) -> dict[str, dict[str, float]]:
    """query id -> {chunk id: BM25 分数}，只含分数 > 0 的 chunk（与 bm25_rankings 的排名集合一致）。"""
    idx = BM25Index([c["text"] for c in chunks])
    out = {}
    for q in queries:
        sc = idx.scores(q["query"])
        out[q["id"]] = {chunks[i]["id"]: s for i, s in enumerate(sc) if s > 0}
    return out


def queries_sha256(path: str = QUERIES_PATH) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_report(rankings: dict[str, list[str]], queries: list[dict], chunks: list[dict], mode: str,
                 scores: dict[str, dict[str, float]] | None = None, queries_path: str = QUERIES_PATH) -> dict:
    """组装 --out 的结果：evaluate() 的原有键（overall/by_type/detail）原样保留，
    另加 mode、target_coverage、no_answer_records 和溯源信息 provenance。"""
    result = evaluate(rankings, queries, chunks)
    result["mode"] = mode
    result["target_coverage"] = target_coverage(rankings, queries, chunks)
    result["no_answer_records"] = no_answer_records(rankings, queries, chunks, mode, scores=scores)
    result["provenance"] = {"mode": mode, "queries_sha256_prefix": queries_sha256(queries_path)[:16]}
    return result


def fake_key_problem() -> str | None:
    """环境里的 OPENAI_API_KEY 若是离线测试/CI 的假 key（sk-fake...），会盖过 .env 里的真 key 导致 401。
    只返回原因，不回显 key。"""
    if os.environ.get("OPENAI_API_KEY", "").startswith("sk-fake"):
        return "当前 shell 里的 OPENAI_API_KEY 是假 key（以 sk-fake 开头），请先 `unset OPENAI_API_KEY`（程序会从 .env 读取）。"
    return None


def check_out_path(path: str | None, force: bool) -> None:
    """--out 已存在且没有 --force 时拒绝（在任何检索/API 工作之前调用）。
    --force 只控制输出文件覆盖，与 API 调用授权无关。"""
    if path and os.path.exists(path) and not force:
        raise SystemExit(f"输出文件已存在：{path}。默认拒绝覆盖；确需覆盖请显式加 --force"
                         "（--force 只影响输出文件，不授权任何 API 调用）。")


def write_result(path: str, text: str, force: bool = False) -> None:
    check_out_path(path, force)
    with open(path, "w" if force else "x", encoding="utf-8") as f:
        f.write(text)


def vector_rank_ids(collection, query: str, pool: int = CANDIDATE_POOL_MAX) -> list[str]:
    """向量候选池：取 min(chunk 总数, pool) 个，按向量距离升序。collection 只需要
    有 count() 和 query(query_texts=, n_results=)（便于用假对象单测）。"""
    n = min(collection.count(), pool)
    res = collection.query(query_texts=[query], n_results=n)
    return list(res["ids"][0])


def hybrid_rankings(queries, chunks, vector_by_query, pool=CANDIDATE_POOL_MAX, rrf_k=RRF_K):
    """BM25 只在向量候选池内参与重排（候选池之外的 chunk 不会被 BM25 找回）。"""
    by_id = {c["id"]: c for c in chunks}
    out = {}
    for q in queries:
        vec = vector_by_query[q["id"]][:pool]
        out[q["id"]] = hybrid_rank(q["query"], vec, [by_id[i]["text"] for i in vec], vec,
                                   top_n=len(vec), rrf_k=rrf_k)
    return out


def _check_against_chroma(collection, chunks):
    got = collection.get(include=["metadatas"])
    chroma = {(m["doc_id"], m["section"]) for m in got["metadatas"]}
    local = {(c["doc_id"], c["section"]) for c in chunks}
    if chroma != local or len(got["ids"]) != len(chunks):
        raise SystemExit(f"本地切分结果与 Chroma 不一致：chroma={len(got['ids'])} 个，本地={len(chunks)} 个。"
                         "请先重新运行 build_knowledge_base.py 或核对切分规则，再评测。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["bm25", "vector", "hybrid"], required=True)
    ap.add_argument("--allow-api", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 --out 文件（不授权 API 调用）")
    args = ap.parse_args()
    check_out_path(args.out, args.force)  # 先于任何检索/API 工作

    chunks, queries = load_chunks_and_queries()
    scores = None
    if args.mode == "bm25":
        rankings = bm25_rankings(queries, chunks)
        scores = bm25_scores(queries, chunks)
    else:
        if not (args.allow_api and os.environ.get("EVAL_RETRIEVAL_ALLOW_API") == "1"):
            raise SystemExit("vector/hybrid 模式会调用真实 Embedding API：需要 --allow-api 且 "
                             "EVAL_RETRIEVAL_ALLOW_API=1。未获批准前请只运行 --mode bm25。")
        import chromadb
        from chromadb.utils import embedding_functions
        from open_deep_research import internal_doc_search as ids_mod
        problem = fake_key_problem()  # import 时 load_dotenv 之后检查，先于任何 embedding 请求
        if problem:
            raise SystemExit(problem)
        ef = embedding_functions.OpenAIEmbeddingFunction(
            api_key=os.environ.get("OPENAI_API_KEY"), model_name="text-embedding-3-small")
        col = chromadb.PersistentClient(path=ids_mod.CHROMA_DIR).get_collection(
            "company_knowledge_base", embedding_function=ef)
        _check_against_chroma(col, chunks)
        vec = {q["id"]: vector_rank_ids(col, q["query"]) for q in queries}
        rankings = vec if args.mode == "vector" else hybrid_rankings(queries, chunks, vec)

    result = build_report(rankings, queries, chunks, args.mode, scores=scores)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        write_result(args.out, text, force=args.force)
    print(json.dumps({"mode": args.mode, "overall": result["overall"], "by_type": result["by_type"]},
                     ensure_ascii=False, indent=1))


def load_chunks_and_queries():
    return load_kb_chunks(), load_queries()


if __name__ == "__main__":
    main()
