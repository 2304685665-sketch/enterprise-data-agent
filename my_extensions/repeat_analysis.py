"""D4 重复运行稳定性分析（PROTOCOL_repeat.md）。只读已有目录，拒绝含模型调用错误的运行。
用法: python repeat_analysis.py [--out PATH]"""
import glob, json, os, statistics, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import agent_eval_rescore_v3 as R3
R = R3.R
RUNS = ["fast_D4", "fast_D4_r2", "fast_D4_r3"]


def calls(d):
    out = {}
    for f in glob.glob(os.path.join(R, d, "case_*.json")):
        j = json.load(open(f, encoding="utf-8")); mc = j.get("model_calls") or {}
        if mc.get("error", 0) or "insufficient_quota" in json.dumps(j):
            raise SystemExit(f"{d}/{os.path.basename(f)} 含模型调用错误/额度耗尽，运行作废")
        out[j["case_id"]] = mc.get("ok", 0)
    return out


if __name__ == "__main__":
    data = {}
    for d in RUNS:
        c = calls(d)
        if len(c) != 63: raise SystemExit(f"{d} 只有 {len(c)} 例，未完成")
        rows = {r["id"]: r for r in R3.rescore3(d)}
        data[d] = {"calls": c, "rows": rows}
    means = [statistics.mean(data[d]["calls"].values()) for d in RUNS]
    res = {"calls_mean_per_run": [round(m, 3) for m in means], "calls_mean_of_runs": round(statistics.mean(means), 3),
           "calls_sd_across_runs": round(statistics.stdev(means), 3), "calls_min_max": [round(min(means), 3), round(max(means), 3)]}
    for ver in ("v1", "v2", "v3"):
        per = [sum(data[d]["rows"][i][ver] for i in data[d]["rows"]) for d in RUNS]
        res[f"pass_{ver}_per_run"] = per
    ids = sorted(data[RUNS[0]]["rows"])
    for ver in ("v1", "v3"):
        allp = sum(all(data[d]["rows"][i][ver] for d in RUNS) for i in ids)
        none = sum(not any(data[d]["rows"][i][ver] for d in RUNS) for i in ids)
        res[f"consistency_{ver}"] = {"pass_all_3": allp, "fail_all_3": none, "flip": len(ids) - allp - none}
    # 每例调用数在三次运行间的平均绝对离差
    res["per_case_calls_mean_range"] = round(statistics.mean(max(data[d]["calls"][i] for d in RUNS) - min(data[d]["calls"][i] for d in RUNS) for i in ids), 3)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(res, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
