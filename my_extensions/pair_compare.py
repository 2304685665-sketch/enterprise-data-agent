"""两个运行目录的配对比较（复用 limits_ablation_analysis.compare）。
用法: python pair_compare.py <A目录名> <B目录名> --out eval_results/xxx.json
目录名相对 eval_results。A 为对照，B 为处理。输出 B-A。"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import limits_ablation_analysis as L
a, b = sys.argv[1], sys.argv[2]
res = L.compare(a, b)
if "--out" in sys.argv:
    out = sys.argv[sys.argv.index("--out") + 1]
    if os.path.exists(out): raise SystemExit(f"拒绝覆盖: {out}")
    json.dump({"a": a, "b": b, **res}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(res, ensure_ascii=False, indent=1))
