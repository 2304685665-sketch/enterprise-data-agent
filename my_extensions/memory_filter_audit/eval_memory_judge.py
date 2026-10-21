"""LLM 判别层评测（PROTOCOL_memory_judge.md）：规则 + 判别器（ODR_MEMORY_LLM_JUDGE=1）在四份冻结语料上的结果。
会发起真实模型调用（gpt-4.1-mini，仅对规则放行的条目），预计 < $0.1；需要 ODR_EVAL_CONFIRM_LIVE=yes。
判别器出错的条目数 > 0 则本次作废。提示词在 memory_guard.JUDGE_PROMPT，评测前冻结，评测后不得改。
用法: ODR_EVAL_CONFIRM_LIVE=yes python eval_memory_judge.py [--out PATH]"""
import hashlib, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
from secret_split import restore
if os.environ.get("ODR_EVAL_CONFIRM_LIVE") != "yes": raise SystemExit("需要 ODR_EVAL_CONFIRM_LIVE=yes（会发起真实模型调用）")
from dotenv import load_dotenv; load_dotenv(os.path.join(HERE, "..", "..", ".env"))
os.environ["ODR_MEMORY_LLM_JUDGE"] = "1"
from open_deep_research import memory_guard as mg

CORPORA = [("dev", "memory_filter_corpus.json"), ("v2", "memory_filter_corpus_v2_heldout.json"),
           ("v3", "memory_filter_corpus_v3_heldout.json"), ("v4", "memory_filter_corpus_v4_heldout.json")]
EXCLUDE = {"dev": {"M45"}}
MAX_CALLS = 700


def run():
    out = {"judge_prompt_sha256": hashlib.sha256(mg.JUDGE_PROMPT.encode()).hexdigest(), "model": os.environ.get(mg.JUDGE_MODEL_ENV) or mg.JUDGE_DEFAULT_MODEL, "corpora": {}}
    for name, fn in CORPORA:
        raw = restore(open(os.path.join(HERE, fn), "rb").read())
        items = [it for it in json.loads(raw)["items"] if it["id"] not in EXCLUDE.get(name, set())]
        rule_only, combined, judge_only_hits = set(), set(), []
        for it in items:
            t = it["memory"]
            rule = mg.contains_injection(t) or mg.contains_sensitive(t)
            if rule: rule_only.add(it["id"]); combined.add(it["id"]); continue
            if mg._JUDGE_STATS["calls"] > MAX_CALLS: raise SystemExit("调用数超过上限，中止")
            if mg.llm_flags(t): combined.add(it["id"]); judge_only_hits.append(it["id"])
        mal = [i for i in items if i["label"] == "malicious"]; ben = [i for i in items if i["label"] == "benign"]
        out["corpora"][name] = {"sha256": hashlib.sha256(raw).hexdigest(), "malicious": len(mal), "benign": len(ben),
            "rules_caught": sum(i["id"] in rule_only for i in mal), "combined_caught": sum(i["id"] in combined for i in mal),
            "rules_false_drop": sum(i["id"] in rule_only for i in ben), "combined_false_drop": sum(i["id"] in combined for i in ben),
            "combined_missed": [i["id"] for i in mal if i["id"] not in combined], "combined_dropped_benign": [i["id"] for i in ben if i["id"] in combined],
            "by_type_combined_caught": {t: [sum(i["id"] in combined for i in mal if i["type"] == t), sum(1 for i in mal if i["type"] == t)] for t in sorted({i["type"] for i in mal})}}
    out["judge_calls"] = mg._JUDGE_STATS["calls"]; out["judge_errors"] = mg._JUDGE_STATS["errors"]
    return out


if __name__ == "__main__":
    r = run(); print(json.dumps(r, ensure_ascii=False, indent=1))
    if r["judge_errors"]: print("判别器出错 %d 次：本次作废，不得引用" % r["judge_errors"], file=sys.stderr)
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(r, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
