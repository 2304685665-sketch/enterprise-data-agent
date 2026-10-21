"""按 PROTOCOL_attack.md 分析两臂：攻击成功数、write_attempted、FALSE_SUCCESS_CLAIM、对照通过数、McNemar 精确检验。
用法: python attack_analysis.py [--out PATH]（读 eval_results/attack_guarded 与 attack_unguarded；未跑完的用例只统计共有部分）"""
import glob, json, math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); R = os.path.join(HERE, "eval_results")
DIRS = {"guarded": os.environ.get("ATTACK_GUARDED_DIR", "attack_guarded_v2"), "unguarded": "attack_unguarded"}  # 第一次 guarded 运行已作废（EVIDENCE §40）
def load(arm):
    out = {}
    for p in glob.glob(os.path.join(R, DIRS[arm], "case_*.json")):
        d = json.load(open(p, encoding="utf-8")); out[d["case_id"]] = d
    return out
def mcnemar(b, c):
    n = b + c
    if n == 0: return 1.0
    k = min(b, c); return round(min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n), 4)
g, u = load("guarded"), load("unguarded")
ids = sorted(set(g) & set(u))
bad = [i for i in ids if g[i].get("run_status") != "finished" or u[i].get("run_status") != "finished"]
atk = [i for i in ids if u[i]["attack"]["is_attack"]]; ctl = [i for i in ids if not u[i]["attack"]["is_attack"]]
def arm(d, sel):
    return dict(n=len(sel), attack_success=sum(d[i]["attack"]["attack_success"] for i in sel),
                write_attempted=sum(d[i]["attack"]["write_attempted"] for i in sel),
                false_success_claim=sum("FALSE_SUCCESS_CLAIM" in d[i].get("issues", []) for i in sel),
                db_changed=sum(not d[i].get("db_sha256_unchanged", True) for i in sel))
res = dict(cases_in_both=len(ids), not_finished=bad, attacks=dict(guarded=arm(g, atk), unguarded=arm(u, atk)),
           controls=dict(guarded=dict(arm(g, ctl), passed=sum(bool(g[i]["score_passed"]) for i in ctl)),
                         unguarded=dict(arm(u, ctl), passed=sum(bool(u[i]["score_passed"]) for i in ctl))))
b = sum(1 for i in atk if u[i]["attack"]["attack_success"] and not g[i]["attack"]["attack_success"])
c = sum(1 for i in atk if g[i]["attack"]["attack_success"] and not u[i]["attack"]["attack_success"])
res["mcnemar"] = dict(unguarded_only_success=b, guarded_only_success=c, p=mcnemar(b, c))
res["unguarded_success_ids"] = [i for i in atk if u[i]["attack"]["attack_success"]]
res["guarded_success_ids"] = [i for i in atk if g[i]["attack"]["attack_success"]]
if "--out" in sys.argv:
    out = sys.argv[sys.argv.index("--out") + 1]
    if os.path.exists(out): raise SystemExit(f"拒绝覆盖: {out}")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
