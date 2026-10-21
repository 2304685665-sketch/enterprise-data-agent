"""v4 结果分析：标题头索引 vs 原索引，按模式做配对 Hit@1 精确符号检验。
确认性（主）：new_v4 单独；补充：new_v3+new_v4 合并；对照：heldout、old_v1。
用法: python retrieval_v4_analysis.py eval_results/retrieval_v4_20261013.json [--out PATH]"""
import json, math, os, sys
def sign_p(b, c):
    n = b + c
    if n == 0: return 1.0
    k = min(b, c); return round(min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n), 4)
r = json.load(open(sys.argv[1], encoding="utf-8"))
def tally(groups, mode):
    gained = lost = better = worse = 0
    for g in groups:
        p = r["paired"][f"{g}|{mode}"]
        gained += len(p["hit1_gained"]); lost += len(p["hit1_lost"]); better += len(p["v2_better_rank"]); worse += len(p["v2_worse_rank"])
    return dict(hit1_gained=gained, hit1_lost=lost, p_hit1=sign_p(gained, lost), rank_better=better, rank_worse=worse, p_rank=sign_p(better, worse))
out = {}
for label, gs in (("confirmatory_new_v4", ["new_v4"]), ("pooled_new_v3_v4", ["new_v3", "new_v4"]), ("heldout", ["heldout"]), ("old_v1", ["old_v1"])):
    out[label] = {m: tally(gs, m) for m in ("bm25", "vector", "hybrid")}
    out[label]["levels"] = {m: {"index_v1": r["results"][f"index_v1|{gs[0]}|{m}"]["overall"] if len(gs) == 1 else None,
                                "index_v2": r["results"][f"index_v2|{gs[0]}|{m}"]["overall"] if len(gs) == 1 else None} for m in ("bm25", "vector", "hybrid")}
print(json.dumps(out, ensure_ascii=False, indent=1))
if "--out" in sys.argv:
    p = sys.argv[sys.argv.index("--out") + 1]
    if os.path.exists(p): raise SystemExit("拒绝覆盖")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
