"""按 PROTOCOL_fast_path.md 汇总 A0/D1/D2/D3/D4（主要比较 D4 vs A0；其余作为分解）。只读已有结果目录，不调用 API。
用法: python fast_path_analysis.py [--out PATH]"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import limits_ablation_analysis as L
ARMS = [("A0", "main2_final"), ("D1", "direct_main2"), ("D2", "fast_D2"), ("D3", "fast_D3"), ("D4", "fast_D4")]
rows = {}
for tag, d in ARMS[1:]:
    r = L.compare("main2_final", d)
    rows[tag] = {"dir": d, "calls": [r["calls"]["mean_a"], r["calls"]["mean_b"], r["calls"]["p"]],
                 "sql": [r["sql"]["mean_a"], r["sql"]["mean_b"], r["sql"]["p"]],
                 "cost": [r["cost"]["mean_a"], r["cost"]["mean_b"], r["cost"]["p"]], "v1": r["v1"], "v2": r["v2"],
                 "ungrounded": r["ungrounded_cases"]}
    print(tag, d, "calls %.2f->%.2f p=%s | sql %.2f->%.2f | cost %.4f->%.4f | v1 %s->%s (A-only %s, B-only %s, p=%s) | v2 %s->%s | ungrounded %s"
          % (*rows[tag]["calls"][:2], rows[tag]["calls"][2], *rows[tag]["sql"][:2], *rows[tag]["cost"][:2],
             r["v1"]["pass_a"], r["v1"]["pass_b"], r["v1"]["a_only_pass"], r["v1"]["b_only_pass"], r["v1"]["mcnemar_exact_p"],
             r["v2"]["pass_a"], r["v2"]["pass_b"], r["ungrounded_cases"]))
d4 = rows["D4"]["v1"]
guard = (d4["pass_b"] >= d4["pass_a"] - 2) and d4["mcnemar_exact_p"] > 0.05 if d4["a_only_pass"] else True
print("质量守门（D4 v1 通过数不比 A0 低超过 2 条，且损失的 McNemar p>0.05）:", "通过" if guard else "未通过 -> 按协议写成速度/质量取舍")
if "--out" in sys.argv:
    out = sys.argv[sys.argv.index("--out") + 1]
    if os.path.exists(out): raise SystemExit(f"拒绝覆盖: {out}")
    json.dump({"arms": rows, "quality_guard_passed": bool(guard)}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
