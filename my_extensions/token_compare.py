"""Token 用量对比（只读 summary.json 的 budget.by_model；输入/输出 token 为 API 返回的 usage，含缓存部分未单独扣除）。
用法: python token_compare.py [--out PATH]"""
import json, os, sys
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results")
PAIRS = [("main2（63 例）", "main2_final", "fast_D4"), ("泛化 gen（40 例）", "gen_G0", "gen_G3")]


def tok(d):
    b = json.load(open(os.path.join(R, d, "summary.json")))["budget"]; m = b["by_model"].values()
    return {"calls": b["calls_ok"], "input": sum(v["input_tokens"] for v in m), "output": sum(v["output_tokens"] for v in m), "usd": b["actual_usd"]}


if __name__ == "__main__":
    out = {}
    for name, a, b in PAIRS:
        A, B = tok(a), tok(b)
        out[name] = {"base": {"dir": a, **A, "total": A["input"] + A["output"]}, "treat": {"dir": b, **B, "total": B["input"] + B["output"]},
                     "total_change_pct": round(100 * ((B["input"] + B["output"]) / (A["input"] + A["output"]) - 1), 1),
                     "input_change_pct": round(100 * (B["input"] / A["input"] - 1), 1), "output_change_pct": round(100 * (B["output"] / A["output"] - 1), 1),
                     "usd_change_pct": round(100 * (B["usd"] / A["usd"] - 1), 1)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(out, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
