"""v2 检索实验：给 chunk 加“文档标题（含公司名）”上下文头，是否改善检索？

设计（先读）：
- 同一套流水线、同一个 embedding 模型，同一批查询，只改“索引文本”：
  v1 索引文本 = 原 chunk 文本；v2 索引文本 = f"文档：{H1 标题}\\n" + 原 chunk 文本（BM25 与向量用同一份文本）。
- 查询集：v1 的 33 条（sha256 前缀固定）+ 冻结的 10 条 held-out（完整 sha256 固定）。哈希不符直接拒绝运行。
  v1 查询上的任何改善都是事后观察（v2 的想法来自对 v1 回退的分析），只有 held-out 才是未参与调参的证据。
- 向量用 numpy-free 的纯 Python 余弦相似度（embedding 已归一化，等价点积），并列时按 chunk 原顺序（稳定排序，预先固定）。
  不使用 Chroma：避免改动已有索引；并给出“流水线复现”核对：重现的 v1 向量指标应与已记录的 Chroma 结果一致，不一致就如实报告。
- API：只在 --allow-api 且 EVAL_RETRIEVAL_ALLOW_API=1 且 key 不是假 key 时运行；一共 4 次 embeddings 请求
  （v1 chunks、v2 chunks、v1 查询、held-out 查询），每次不重试；预算上限默认 $0.01，运行前用保守的 token 上界预估，
  超过就拒绝；实际费用按返回的 usage.total_tokens × $0.02/1M 计算并记录。
- 输出文件用 "x" 模式创建，已存在则拒绝，不覆盖任何已有结果。
- 局限：25 个 chunk、8 条 held-out 有答案查询，差异多半不显著；不外推到生产知识库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import eval_retrieval as ER  # noqa: E402

V1_QUERIES = ER.QUERIES_PATH
HELDOUT_QUERIES = os.path.join(HERE, "eval_retrieval_queries_v2_heldout.json")
V1_SHA_PREFIX = "f24a5e0346995cb4"
HELDOUT_SHA = "dfcb7d78fbe9d4d8e2d626468b2a90e67db9931811a30cee5dcbdb2dfcc0059f"
MODEL = "text-embedding-3-small"
PRICE_PER_M_TOKENS = 0.02  # 标准档，需开跑当天核对
MODES = ("bm25", "vector", "hybrid")
RECORDED_V1_VECTOR = os.path.join(HERE, "eval_results", "retrieval_vector_v1_20261012.json")


def doc_titles(kb_dir: str = ER.KB_DIR) -> dict[str, str]:
    out = {}
    for fn in sorted(os.listdir(kb_dir)):
        if fn.endswith(".md") and fn != "ANSWER_KEY.md":
            m = re.search(r"^# (.+)$", open(os.path.join(kb_dir, fn), encoding="utf-8").read(), re.M)
            out[fn[:-3]] = m.group(1).strip() if m else fn[:-3]
    return out


def v2_chunks(chunks: list[dict], titles: dict[str, str]) -> list[dict]:
    return [{**c, "text": f"文档：{titles[c['doc_id']]}\n{c['text']}"} for c in chunks]


def check_query_files() -> None:
    if ER.queries_sha256(V1_QUERIES)[:16] != V1_SHA_PREFIX:
        raise SystemExit("v1 查询集哈希与冻结值不符，拒绝运行。")
    if ER.queries_sha256(HELDOUT_QUERIES) != HELDOUT_SHA:
        raise SystemExit("held-out 查询集哈希与冻结值不符，拒绝运行。")


def estimate_cost_usd(texts: list[str]) -> float:
    """保守上界：每个字符按 2 个 token 计（实际中文约 0.6~1 token/字符，英文更少）。"""
    return sum(len(t) for t in texts) * 2 * PRICE_PER_M_TOKENS / 1e6


def cosine_rank(chunk_vecs: dict[str, list[float]], qvec: list[float], order: list[str]) -> list[str]:
    def norm(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]
    q = norm(qvec)
    sims = [(sum(a * b for a, b in zip(norm(chunk_vecs[cid]), q)), -i, cid) for i, cid in enumerate(order)]
    sims.sort(reverse=True)  # 相似度降序；并列时 -i 大者(原顺序靠前)在前
    return [cid for _, _, cid in sims]


def rank_all(chunks: list[dict], queries: list[dict], chunk_vecs, query_vecs) -> dict[str, dict[str, list[str]]]:
    order = [c["id"] for c in chunks]
    vec = {q["id"]: cosine_rank(chunk_vecs, query_vecs[q["id"]], order) for q in queries}
    return {"bm25": ER.bm25_rankings(queries, chunks), "vector": vec, "hybrid": ER.hybrid_rankings(queries, chunks, vec)}


def summarize(rankings, queries, chunks, mode, exclude: set[str] = frozenset()):
    qs = [q for q in queries if q["id"] not in exclude]
    res = ER.evaluate(rankings, qs, chunks)
    return {"overall": res["overall"], "by_type": res["by_type"], "detail": res["detail"],
            "target_coverage": ER.target_coverage(rankings, qs, chunks),
            "no_answer_records": ER.no_answer_records(rankings, qs, chunks, mode)}


def run_experiment(embed, chunks_v1: list[dict], titles: dict[str, str], v1_queries: list[dict], held: list[dict]) -> dict:
    """embed(texts: list[str]) -> (vectors, total_tokens)。便于用假 embed 做离线测试。"""
    chunks_v2 = v2_chunks(chunks_v1, titles)
    usage = {}
    cv = {}
    for name, ch in (("index_v1", chunks_v1), ("index_v2", chunks_v2)):
        vs, tok = embed([c["text"] for c in ch]); usage[f"chunks_{name}"] = tok
        cv[name] = {c["id"]: v for c, v in zip(ch, vs)}
    qv = {}
    for name, qs in (("v1_queries", v1_queries), ("heldout", held)):
        vs, tok = embed([q["query"] for q in qs]); usage[f"queries_{name}"] = tok
        qv[name] = {q["id"]: v for q, v in zip(qs, vs)}
    results = {}
    for iname, ch in (("index_v1", chunks_v1), ("index_v2", chunks_v2)):
        for qname, qs in (("v1_queries", v1_queries), ("heldout", held)):
            rk = rank_all(ch, qs, cv[iname], qv[qname])
            for mode in MODES:
                results[f"{iname}|{qname}|{mode}"] = summarize(rk[mode], qs, ch, mode)
                if qname == "heldout":
                    results[f"{iname}|heldout_without_H08|{mode}"] = summarize(rk[mode], qs, ch, mode, {"H08"})
                    results[f"{iname}|heldout_without_H03|{mode}"] = summarize(rk[mode], qs, ch, mode, {"H03"})
    paired = {}
    for qname in ("v1_queries", "heldout"):
        for mode in MODES:
            a = {d["id"]: d["first_hit_rank"] for d in results[f"index_v1|{qname}|{mode}"]["detail"]}
            b = {d["id"]: d["first_hit_rank"] for d in results[f"index_v2|{qname}|{mode}"]["detail"]}
            better = sorted(i for i in a if (b[i] or 99) < (a[i] or 99)); worse = sorted(i for i in a if (b[i] or 99) > (a[i] or 99))
            paired[f"{qname}|{mode}"] = {"v2_better": better, "v2_worse": worse, "unchanged": len(a) - len(better) - len(worse),
                                         "ranks_v1_v2": {i: [a[i], b[i]] for i in a}}
    return {"usage_tokens": usage, "total_tokens": sum(usage.values()), "results": results, "paired": paired}


def reproduction_check(report: dict, recorded_path: str = RECORDED_V1_VECTOR) -> dict:
    """v1 索引 + v1 查询 + 向量：与此前用 Chroma 记录的结果比较（Hit@1 / MRR）。"""
    if not os.path.exists(recorded_path):
        return {"checked": False, "reason": "没有找到已记录的 v1 向量结果文件"}
    old = json.load(open(recorded_path, encoding="utf-8"))["overall"]
    new = report["results"]["index_v1|v1_queries|vector"]["overall"]
    same = abs(old["hit1"] - new["hit1"]) < 1e-9 and abs(old["mrr"] - new["mrr"]) < 1e-9
    return {"checked": True, "recorded": old, "reproduced": {"hit1": new["hit1"], "mrr": new["mrr"]}, "identical": same}


def openai_embed(client):
    def embed(texts):
        r = client.embeddings.create(model=MODEL, input=texts)
        vs = [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
        return vs, int(r.usage.total_tokens)
    return embed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--allow-api", action="store_true")
    ap.add_argument("--budget-usd", type=float, default=0.01)
    args = ap.parse_args(argv)

    ER.check_out_path(args.out, False)
    check_query_files()
    if not (args.allow_api and os.environ.get("EVAL_RETRIEVAL_ALLOW_API") == "1"):
        raise SystemExit("本实验会调用真实 Embedding API：需要 --allow-api 且 EVAL_RETRIEVAL_ALLOW_API=1。")
    problem = ER.fake_key_problem()
    if problem:
        raise SystemExit(problem)
    if args.budget_usd <= 0:
        raise SystemExit("--budget-usd 必须 > 0")

    chunks = ER.load_kb_chunks(); titles = doc_titles()
    v1q = ER.load_queries(V1_QUERIES); held = ER.load_queries(HELDOUT_QUERIES)
    texts = [c["text"] for c in chunks] + [c["text"] for c in v2_chunks(chunks, titles)] + [q["query"] for q in v1q + held]
    est = estimate_cost_usd(texts)
    print(f"预检：{len(texts)} 条文本、4 次 embeddings 请求；保守费用上界 ${est:.6f}；预算 ${args.budget_usd}")
    if est > args.budget_usd:
        raise SystemExit("保守费用上界超过预算，拒绝运行。")

    from dotenv import load_dotenv
    load_dotenv()
    if os.environ.get("OPENAI_API_KEY", "").startswith("sk-fake"):
        raise SystemExit(ER.fake_key_problem())
    import openai
    report = run_experiment(openai_embed(openai.OpenAI()), chunks, titles, v1q, held)
    cost = report["total_tokens"] * PRICE_PER_M_TOKENS / 1e6
    report["provenance"] = {
        "model": MODEL, "price_per_m_tokens_usd": PRICE_PER_M_TOKENS, "requests": 4, "texts_embedded": len(texts),
        "actual_cost_usd": cost, "estimated_upper_bound_usd": est, "budget_usd": args.budget_usd,
        "v1_queries_sha256_prefix": ER.queries_sha256(V1_QUERIES)[:16], "heldout_queries_sha256": ER.queries_sha256(HELDOUT_QUERIES),
        "python": platform.python_version(), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "v2_text_format": "文档：{H1 标题}\\n{原 chunk 文本}", "tie_break": "vector: original chunk order (stable)",
    }
    report["reproduction_check"] = reproduction_check(report)
    ER.write_result(args.out, json.dumps(report, ensure_ascii=False, indent=1))
    print(f"完成：实际 {report['total_tokens']} tokens ≈ ${cost:.6f}；结果写入 {args.out}")
    for key in sorted(report["results"]):
        if "without" in key:
            continue
        o = report["results"][key]["overall"]
        print(f"{key:36s} n={o['n']:2d} Hit@1={o['hit1']:.3f} MRR={o['mrr']:.3f}")
    print("复现核对：", json.dumps(report["reproduction_check"], ensure_ascii=False))


if __name__ == "__main__":
    main()
