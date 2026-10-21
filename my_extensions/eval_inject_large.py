"""间接注入防护规模化离线评测（PROTOCOL_inject_large.md）。用法: python eval_inject_large.py [--out PATH]"""
import json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
JUDGE = "--judge" in sys.argv
if not JUDGE:
    os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")   # 离线模式：不需要真实密钥
if JUDGE:   # 会发起真实模型调用（gpt-4.1-mini，每个 chunk 一次），预计 < $0.1
    if os.environ.get("ODR_EVAL_CONFIRM_LIVE") != "yes": raise SystemExit("--judge 需要 ODR_EVAL_CONFIRM_LIVE=yes")
    from dotenv import load_dotenv; load_dotenv(os.path.join(HERE, "..", ".env"))   # 注意：必须在设置任何假密钥之前加载（load_dotenv 默认不覆盖已有变量）
    if not os.environ.get("OPENAI_API_KEY") or os.environ["OPENAI_API_KEY"].startswith("fake"): raise SystemExit("--judge 需要真实的 OPENAI_API_KEY（从 .env 读取）")
    os.environ["ODR_DOC_LLM_JUDGE"] = "1"
import hashlib
import eval_retrieval as ER, inject_corpus_large as C
from open_deep_research import doc_content_guard as G

EXPECTED_SHA = "83ece48990be4ccb7b2b440f52c9a375d0e8486ca6602a84b5b51a399173f43e"


def run():
    if C.corpus_sha() != EXPECTED_SHA: raise SystemExit("语料 sha 与冻结值不符")
    kb = ER.load_kb_chunks(os.path.join(HERE, "knowledge_base"))
    rows = []
    for r in C.build(kb):
        out, n = G.sanitize_chunk(r["text"])
        survives = r["inj"] in out
        base_sents = [p for p in re.split(r"(?<=[。！？!?；;\n])", r["base"]) if p.strip()]
        kept = sum(s in out for s in base_sents) / max(1, len(base_sents))
        rows.append({"kind": r["kind"], "cat": r["cat"], "inj": r["inj"], "blocked": not survives, "chunk_fully_masked": out == G.REMOVED, "fact_retention": kept})
    res = {"corpus_sha256": C.corpus_sha(), "judge": JUDGE, "by_cat": {}}
    if JUDGE:
        res["judge_prompt_sha256"] = hashlib.sha256(G.JUDGE_PROMPT.encode()).hexdigest(); res["judge_model"] = os.environ.get(G.JUDGE_MODEL_ENV) or G.JUDGE_DEFAULT_MODEL
    res["real_chunks_modified"] = sum(G.sanitize_chunk(c["text"])[1] > 0 for c in kb)   # 46 个未被投毒的真实 chunk 被改动的数量（误屏蔽）
    if JUDGE:
        res["judge_calls"] = G._JUDGE_STATS["calls"]; res["judge_errors"] = G._JUDGE_STATS["errors"]; res["judge_last_error"] = G._JUDGE_STATS["last_error"]
        if res["judge_errors"]: print("判别器出错 %d/%d 次（最后一次：%s）：本次作废" % (res["judge_errors"], res["judge_calls"], res["judge_last_error"]), file=sys.stderr)
    for cat in sorted({r["cat"] for r in rows}):
        rs = [r for r in rows if r["cat"] == cat]
        res["by_cat"][cat] = {"n": len(rs), "blocked": sum(r["blocked"] for r in rs)}
    inj = [r for r in rows if r["kind"] == "inj"]; ben = [r for r in rows if r["kind"] == "benign"]
    res["injections"] = {"n": len(inj), "blocked": sum(r["blocked"] for r in inj), "chunk_fully_masked": sum(r["chunk_fully_masked"] for r in inj),
                         "mean_fact_retention": round(sum(r["fact_retention"] for r in inj) / len(inj), 3)}
    res["benign_controls"] = {"n": len(ben), "falsely_blocked": sum(r["blocked"] for r in ben), "chunk_fully_masked": sum(r["chunk_fully_masked"] for r in ben),
                              "falsely_blocked_examples": [r["inj"] for r in ben if r["blocked"]]}
    res["injections_missed_examples"] = [r["inj"] for r in inj if not r["blocked"]][:12]
    return res


if __name__ == "__main__":
    r = run(); print(json.dumps(r, ensure_ascii=False, indent=1))
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(r, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
