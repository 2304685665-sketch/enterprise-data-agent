"""按 PROTOCOL_limits_ablation.md 的预先指定检验做分析（纯标准库，无 scipy）。
用法: python limits_ablation_analysis.py <A目录> <B2目录> <B1目录> [--out PATH]
- 模型调用数/费用：配对 Wilcoxon 符号秩（零差异剔除，平均秩处理并列，正态近似带连续性校正）
- 通过与否：McNemar 精确检验（二项，双侧）
- 同时给出 B1 vs B2（运行间波动）
"""
import json, math, os, sys, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent_eval_compare as cmp  # noqa: E402
import agent_eval_rescore_v2 as rs  # noqa: E402


def wilcoxon(diffs):
    d = [x for x in diffs if abs(x) > 1e-12]
    n = len(d)
    if n == 0:
        return {"n_nonzero": 0, "p": 1.0}
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    tie_term = 0.0
    while i < n:
        j = i
        while j + 1 < n and abs(abs(d[order[j + 1]]) - abs(d[order[i]])) < 1e-12:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    wp = sum(r for r, x in zip(ranks, d) if x > 0)
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if var <= 0:
        return {"n_nonzero": n, "p": 1.0}
    z = (abs(wp - mean) - 0.5) / math.sqrt(var)
    p = math.erfc(max(z, 0) / math.sqrt(2))
    return {"n_nonzero": n, "W_plus": wp, "z": round(z, 3), "p": round(min(1.0, p), 4)}


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n * 2
    return round(min(1.0, p), 4)


def per_case(d):
    _, rows = cmp.stats(d)
    return {r["id"]: r for r in rows}


def compare(a, b):
    ra, rb = per_case(a), per_case(b)
    ids = sorted(set(ra) & set(rb))
    out = {"n": len(ids)}
    for key in ("calls", "cost", "sql"):
        da = [(rb[i][key] or 0) - (ra[i][key] or 0) for i in ids]
        out[key] = {"mean_a": round(statistics.mean((ra[i][key] or 0) for i in ids), 4),
                    "mean_b": round(statistics.mean((rb[i][key] or 0) for i in ids), 4),
                    "mean_diff_b_minus_a": round(statistics.mean(da), 4),
                    "median_diff": round(statistics.median(da), 4), **wilcoxon(da)}
    for name, getter in (("v1", lambda r: bool(r["passed"])),):
        b_only = sum(1 for i in ids if getter(rb[i]) and not getter(ra[i]))
        a_only = sum(1 for i in ids if getter(ra[i]) and not getter(rb[i]))
        out[name] = {"pass_a": sum(getter(ra[i]) for i in ids), "pass_b": sum(getter(rb[i]) for i in ids),
                     "a_only_pass": a_only, "b_only_pass": b_only, "mcnemar_exact_p": mcnemar_exact(a_only, b_only)}
    va = {r["id"]: bool(r["v2"]) for r in rs.rescore(a)}
    vb = {r["id"]: bool(r["v2"]) for r in rs.rescore(b)}
    ao = sum(1 for i in ids if va[i] and not vb[i]); bo = sum(1 for i in ids if vb[i] and not va[i])
    out["v2"] = {"pass_a": sum(va[i] for i in ids), "pass_b": sum(vb[i] for i in ids),
                 "a_only_pass": ao, "b_only_pass": bo, "mcnemar_exact_p": mcnemar_exact(ao, bo)}
    out["ungrounded_cases"] = {"a": sum(ra[i]["ung"] for i in ids), "b": sum(rb[i]["ung"] for i in ids)}
    return out


if __name__ == "__main__":
    A, B2, B1 = sys.argv[1:4]
    res = {"A_vs_B2": compare(A, B2), "B1_vs_B2_runtorun": compare(B1, B2)}
    out = None
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(out):
            raise SystemExit(f"拒绝覆盖: {out}")
        json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))
