"""泛化检验分析（PROTOCOL_generalization.md）：G1、G2 各对 G0。只读已有目录，不调用 API。
用法: python gen_analysis.py [--base gen_G0] [--arms gen_G1 gen_G2] [--out PATH]
会先检查各目录没有模型调用错误（额度/限流污染），有则拒绝出结论。"""
import glob, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import limits_ablation_analysis as L
R = os.path.join(HERE, "eval_results")
def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
base = arg("--base", "gen_G0")
arms = sys.argv[sys.argv.index("--arms") + 1:] if "--arms" in sys.argv else ["gen_G1", "gen_G2"]
arms = [a for a in arms if not a.startswith("--") and a != arg("--out", "")]
def contaminated(d):
    bad = 0
    for p in glob.glob(os.path.join(R, d, "case_*.json")):
        r = json.load(open(p, encoding="utf-8"))
        if r.get("run_status") != "finished" or (r.get("model_calls") or {}).get("error", 0) > 0: bad += 1
    return bad
out = {}
for a in [base] + arms:
    n = len(glob.glob(os.path.join(R, a, "case_*.json"))); bad = contaminated(a)
    print(f"{a}: cases={n} contaminated={bad}")
    if bad: raise SystemExit(f"{a} 有 {bad} 例带模型调用错误，按协议作废，不出结论。")
for a in arms:
    r = L.compare(base, a)
    out[a] = r
    print(f"\n{base} -> {a}")
    for k in ("calls", "sql", "cost"):
        print(f"  {k}: {r[k]['mean_a']:.4g} -> {r[k]['mean_b']:.4g}  p={r[k]['p']}  median diff={r[k]['median_diff']}")
    print("  v1:", r["v1"], "\n  v2:", r["v2"], "\n  ungrounded:", r["ungrounded_cases"])
if "--out" in sys.argv:
    p = arg("--out", "")
    if os.path.exists(p): raise SystemExit("拒绝覆盖")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
