"""检索确认性实验 v4：在 v3 之上再加 60 条冻结的新查询（统计功效）。

做法与 eval_retrieval_v3 完全相同（同一知识库 46 chunk、同一 embedding、同一流水线），只是查询组多一个 new_v4。
说明：v3 的新查询是在这条流水线上第一次被观察的；v4 在写成时作者已看过 v3 的结果摘要，所以 v4 只用作“确认”，
主结论只看 new_v4 单独一组（确认性），new_v3+new_v4 合并只作补充。
流水线、预算预检、"x" 模式写文件都复用 v3/v2；约 $0.0003。
用法: EVAL_RETRIEVAL_ALLOW_API=1 python my_extensions/eval_retrieval_v4.py --allow-api --out PATH
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
import eval_retrieval_v3 as V3  # noqa: E402

V4_QUERIES = os.path.join(HERE, "eval_retrieval_queries_v4_large.json")
V4_SHA = "f3d5dfa730e880fab8b22998d735822b134d8cfc06c6c9e8bed239876dce221c"


def check_inputs():
    V3.check_inputs()
    if ER.queries_sha256(V4_QUERIES) != V4_SHA:
        raise SystemExit("v4 查询集哈希与冻结值不符，拒绝运行。")


def load_groups():
    g = V3.load_groups()
    g["new_v4"] = ER.load_queries(V4_QUERIES)
    return g


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--allow-api", action="store_true")
    ap.add_argument("--budget-usd", type=float, default=0.02)
    args = ap.parse_args(argv)
    ER.check_out_path(args.out, False)
    check_inputs()
    if not (args.allow_api and os.environ.get("EVAL_RETRIEVAL_ALLOW_API") == "1"):
        raise SystemExit("需要 --allow-api 且 EVAL_RETRIEVAL_ALLOW_API=1。")
    problem = ER.fake_key_problem()
    if problem:
        raise SystemExit(problem)
    chunks = ER.load_kb_chunks(V3.KB_V3)
    if len(chunks) != V3.EXPECTED_CHUNKS:
        raise SystemExit(f"chunk 数 {len(chunks)} != 冻结值 {V3.EXPECTED_CHUNKS}")
    titles = V2.doc_titles(V3.KB_V3)
    groups = load_groups()
    allq = [q for qs in groups.values() for q in qs]
    texts = [c["text"] for c in chunks] * 2 + [q["query"] for q in allq]
    est = V2.estimate_cost_usd(texts)
    print(f"预检：{len(texts)} 条文本；保守费用上界 ${est:.6f}；预算 ${args.budget_usd}")
    if est > args.budget_usd:
        raise SystemExit("保守费用上界超过预算，拒绝运行。")
    from dotenv import load_dotenv
    load_dotenv()
    import openai
    report = V3.run_experiment(V2.openai_embed(openai.OpenAI()), chunks, titles, groups)
    cost = report["total_tokens"] * V2.PRICE_PER_M_TOKENS / 1e6
    report["provenance"] = {"model": V2.MODEL, "actual_cost_usd": cost, "estimated_upper_bound_usd": est,
                            "v3_queries_sha256": V3.NEW_SHA, "v4_queries_sha256": V4_SHA,
                            "python": platform.python_version(), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    ER.write_result(args.out, json.dumps(report, ensure_ascii=False, indent=1))
    for key in sorted(report["results"]):
        if "|new_v4|" in key:
            o = report["results"][key]["overall"]
            print(f"{key:34s} n={o['n']:2d} Hit@1={o['hit1']:.3f} MRR={o['mrr']:.3f}")


if __name__ == "__main__":
    main()
