"""v3 检索实验（大知识库）：46 个 chunk / 87 条查询，对比 v1 索引与“标题上下文头”索引。

与 eval_retrieval_v2.py 的区别：知识库换成 knowledge_base_v3（原 25 个 chunk + doc11–doc17 的 21 个），
查询集 = v1 的 33 条 + 冻结的 10 条 held-out + 冻结的 44 条新查询（eval_retrieval_queries_v3_large.json，
完整 sha256 固定，写定于看到任何检索排名之前）。三组查询分开报告：
  old_v1（33，v2 时代已被观察过）/ heldout（10，v2 时代未调参）/ new_v3（44，本次新写，从未被观察）。
主结论只看 new_v3；其余两组仅作对照。
流水线（余弦、并列按原顺序、不重试、预算预检、"x" 模式写文件）与 v2 完全相同，直接复用其函数。
局限：知识库仍是人工合成的 46 块，不代表生产；差异多半不显著，如实报告。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import eval_retrieval as ER  # noqa: E402
import eval_retrieval_v2 as V2  # noqa: E402

KB_V3 = os.path.join(HERE, "knowledge_base_v3")
NEW_QUERIES = os.path.join(HERE, "eval_retrieval_queries_v3_large.json")
NEW_SHA = "1e993dd6fcc08e2b4bfb9c78d123ed8f51966220007607e49ae6c1dcc55fba80"
EXPECTED_CHUNKS = 46
GROUPS = ("old_v1", "heldout", "new_v3")
MODES = V2.MODES


def check_inputs() -> None:
    V2.check_query_files()
    if ER.queries_sha256(NEW_QUERIES) != NEW_SHA:
        raise SystemExit("v3 新查询集哈希与冻结值不符，拒绝运行。")


def load_groups() -> dict[str, list[dict]]:
    return {"old_v1": ER.load_queries(V2.V1_QUERIES), "heldout": ER.load_queries(V2.HELDOUT_QUERIES),
            "new_v3": ER.load_queries(NEW_QUERIES)}


def run_experiment(embed, chunks, titles, groups) -> dict:
    chunks_v2 = V2.v2_chunks(chunks, titles)
    usage, cv = {}, {}
    for name, ch in (("index_v1", chunks), ("index_v2", chunks_v2)):
        vs, tok = embed([c["text"] for c in ch]); usage[f"chunks_{name}"] = tok
        cv[name] = {c["id"]: v for c, v in zip(ch, vs)}
    qv = {}
    for g, qs in groups.items():
        vs, tok = embed([q["query"] for q in qs]); usage[f"queries_{g}"] = tok
        qv[g] = {q["id"]: v for q, v in zip(qs, vs)}
    results, paired = {}, {}
    ranks = {}
    for iname, ch in (("index_v1", chunks), ("index_v2", chunks_v2)):
        for g, qs in groups.items():
            rk = V2.rank_all(ch, qs, cv[iname], qv[g])
            for mode in MODES:
                s = V2.summarize(rk[mode], qs, ch, mode)
                results[f"{iname}|{g}|{mode}"] = s
                ranks[(iname, g, mode)] = {d["id"]: d["first_hit_rank"] for d in s["detail"]}
    for g in groups:
        for mode in MODES:
            a, b = ranks[("index_v1", g, mode)], ranks[("index_v2", g, mode)]
            better = sorted(i for i in a if (b[i] or 99) < (a[i] or 99))
            worse = sorted(i for i in a if (b[i] or 99) > (a[i] or 99))
            h1_better = sorted(i for i in a if b[i] == 1 and a[i] != 1)
            h1_worse = sorted(i for i in a if a[i] == 1 and b[i] != 1)
            paired[f"{g}|{mode}"] = {"v2_better_rank": better, "v2_worse_rank": worse,
                                     "hit1_gained": h1_better, "hit1_lost": h1_worse,
                                     "ranks_v1_v2": {i: [a[i], b[i]] for i in a}}
    return {"usage_tokens": usage, "total_tokens": sum(usage.values()), "results": results, "paired": paired}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--allow-api", action="store_true")
    ap.add_argument("--budget-usd", type=float, default=0.02)
    ap.add_argument("--bm25-only", action="store_true", help="完全离线，只算 BM25（v1 与 v2 索引）")
    args = ap.parse_args(argv)
    ER.check_out_path(args.out, False)
    check_inputs()
    chunks = ER.load_kb_chunks(KB_V3)
    if len(chunks) != EXPECTED_CHUNKS:
        raise SystemExit(f"chunk 数 {len(chunks)} != 冻结值 {EXPECTED_CHUNKS}")
    titles = V2.doc_titles(KB_V3)
    groups = load_groups()
    allq = [q for qs in groups.values() for q in qs]
    if args.bm25_only:
        chunks_v2 = V2.v2_chunks(chunks, titles)
        res = {}
        for iname, ch in (("index_v1", chunks), ("index_v2", chunks_v2)):
            for g, qs in groups.items():
                res[f"{iname}|{g}|bm25"] = V2.summarize(ER.bm25_rankings(qs, ch), qs, ch, "bm25")
        report = {"mode": "bm25-only(offline)", "results": res}
    else:
        if not (args.allow_api and os.environ.get("EVAL_RETRIEVAL_ALLOW_API") == "1"):
            raise SystemExit("需要 --allow-api 且 EVAL_RETRIEVAL_ALLOW_API=1（或用 --bm25-only 离线）。")
        problem = ER.fake_key_problem()
        if problem:
            raise SystemExit(problem)
        texts = [c["text"] for c in chunks] * 2 + [q["query"] for q in allq]
        est = V2.estimate_cost_usd(texts)
        print(f"预检：{len(texts)} 条文本；保守费用上界 ${est:.6f}；预算 ${args.budget_usd}")
        if est > args.budget_usd:
            raise SystemExit("保守费用上界超过预算，拒绝运行。")
        from dotenv import load_dotenv
        load_dotenv()
        if os.environ.get("OPENAI_API_KEY", "").startswith("sk-fake"):
            raise SystemExit(ER.fake_key_problem())
        import openai
        report = run_experiment(V2.openai_embed(openai.OpenAI()), chunks, titles, groups)
        cost = report["total_tokens"] * V2.PRICE_PER_M_TOKENS / 1e6
        report["provenance"] = {"model": V2.MODEL, "actual_cost_usd": cost, "estimated_upper_bound_usd": est,
                                "budget_usd": args.budget_usd, "n_chunks": len(chunks),
                                "new_queries_sha256": NEW_SHA, "python": platform.python_version(),
                                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    ER.write_result(args.out, json.dumps(report, ensure_ascii=False, indent=1))
    for key in sorted(report["results"]):
        o = report["results"][key]["overall"]
        print(f"{key:34s} n={o['n']:2d} Hit@1={o['hit1']:.3f} MRR={o['mrr']:.3f}")


if __name__ == "__main__":
    main()
