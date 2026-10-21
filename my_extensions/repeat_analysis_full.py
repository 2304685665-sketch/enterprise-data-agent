"""基线 vs D4 各 3 次运行的比较（PROTOCOL_repeat.md 补充）。拒绝含模型调用错误的运行。
用法: python repeat_analysis_full.py [--out PATH]"""
import glob, json, math, os, statistics, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import agent_eval_rescore_v3 as R3
R = R3.R
BASE = ["main2_final", "base_main2_r2", "base_main2_r3"]; D4 = ["fast_D4", "fast_D4_r2", "fast_D4_r3"]


def calls(d):
    out = {}
    for f in glob.glob(os.path.join(R, d, "case_*.json")):
        j = json.load(open(f, encoding="utf-8")); mc = j.get("model_calls") or {}
        if mc.get("error", 0) or "insufficient_quota" in json.dumps(j): raise SystemExit(f"{d} 含模型调用错误/额度耗尽，运行作废")
        out[j["case_id"]] = mc.get("ok", 0)
    if len(out) != 63: raise SystemExit(f"{d} 只有 {len(out)} 例")
    return out


def wilcoxon_p(x, y):
    """配对 Wilcoxon 符号秩检验，双侧，精确（n 小时）或正态近似；零差丢弃。"""
    d = [a - b for a, b in zip(x, y) if a != b]
    n = len(d)
    if n == 0: return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i])); ranks = [0.0] * n; i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]): j += 1
        for k in range(i, j + 1): ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    wp = sum(r for r, v in zip(ranks, d) if v > 0); mu = n * (n + 1) / 4; sd = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    z = (wp - mu) / sd if sd else 0.0
    return math.erfc(abs(z) / math.sqrt(2))


def summarize(dirs):
    cs = [calls(d) for d in dirs]; rows = [{r["id"]: r for r in R3.rescore3(d)} for d in dirs]
    means = [statistics.mean(c.values()) for c in cs]
    out = {"dirs": dirs, "calls_mean_per_run": [round(m, 3) for m in means], "calls_mean": round(statistics.mean(means), 3), "calls_sd_across_runs": round(statistics.stdev(means), 3)}
    for v in ("v1", "v2", "v3"): out[f"pass_{v}_per_run"] = [sum(r[i][v] for i in r) for r in rows]
    return out, cs, rows


if __name__ == "__main__":
    B, bc, br = summarize(BASE); D, dc, dr = summarize(D4)
    ids = sorted(br[0])
    res = {"baseline": B, "d4": D, "calls_reduction_pct_of_means": round(100 * (D["calls_mean"] / B["calls_mean"] - 1), 1)}
    for v in ("v1", "v3"):
        pb = [statistics.mean(r[i][v] for r in br) for i in ids]; pd = [statistics.mean(r[i][v] for r in dr) for i in ids]
        res[f"per_case_pass_rate_{v}"] = {"baseline_mean": round(statistics.mean(pb), 4), "d4_mean": round(statistics.mean(pd), 4), "wilcoxon_p_two_sided": round(wilcoxon_p(pd, pb), 4),
                                         "d4_better_cases": sum(a > b for a, b in zip(pd, pb)), "baseline_better_cases": sum(b > a for a, b in zip(pd, pb))}
    pc = [(statistics.mean(c[i] for c in bc), statistics.mean(c[i] for c in dc)) for i in ids]
    res["per_case_calls_wilcoxon_p"] = wilcoxon_p([b for b, _ in pc], [d for _, d in pc])
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(res, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
