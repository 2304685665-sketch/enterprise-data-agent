"""统计函数留出实验分析（PROTOCOL_stats_funcs.md）：S1 对 S0。只读已有目录，拒绝含模型调用错误的运行。
用法: python stats_analysis.py [--out PATH]"""
import glob, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import limits_ablation_analysis as L
R = os.path.join(HERE, "eval_results")


def contaminated(d):
    bad = []
    for f in glob.glob(os.path.join(R, d, "case_*.json")):
        j = json.load(open(f, encoding="utf-8")); mc = j.get("model_calls")
        s = json.dumps(j)
        if (isinstance(mc, dict) and mc.get("error", 0)) or "insufficient_quota" in s or "RateLimitError" in s:
            bad.append(os.path.basename(f))
    return bad


def tool_stats(d):
    py = nof = 0
    for f in glob.glob(os.path.join(R, d, "case_*.json")):
        j = json.load(open(f, encoding="utf-8")); ev = j.get("evidence") or {}
        py += (ev.get("tool_call_counts") or {}).get("python_executor", 0)
    for f in glob.glob(os.path.join(R, d, "trace", "*", "run_trace.jsonl")):
        for ln in open(f, encoding="utf-8"):
            if "no such function" in ln: nof += 1
    return py, nof


if __name__ == "__main__":
    for d in ("stats_S0", "stats_S1"):
        n = len(glob.glob(os.path.join(R, d, "case_*.json"))); bad = contaminated(d)
        print(d, "cases", n, "contaminated", bad)
        if n != 12 or bad: raise SystemExit("运行不完整或含错误，按协议作废，不分析")
    r = L.compare("stats_S0", "stats_S1")
    print("calls", r["calls"]); print("cost", r["cost"]); print("v1", r["v1"]); print("v2", r["v2"])
    for d in ("stats_S0", "stats_S1"):
        py, nof = tool_stats(d); print(d, "python_executor 调用", py, "| 'no such function' 报错", nof)
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(out): raise SystemExit(f"拒绝覆盖: {out}")
        json.dump(r, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
