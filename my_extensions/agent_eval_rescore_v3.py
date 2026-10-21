"""用评分器 v1 / v2 / v3 对已有运行目录离线重评（不调用 API、不改动运行目录）。
v1=预注册；v2=事后（no_answer / refuse_write）；v3=事后（年份答案、计数量词，见 agent_eval_score_v3.py）。
用法: python agent_eval_rescore_v3.py <运行目录名> [...]"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_eval_cases, agent_eval_cases_heldout as hc, agent_eval_cases_large as lc, agent_eval_cases_gen as gc, agent_eval_cases_stats as sc
import agent_eval_score as v1, agent_eval_score_v2 as v2, agent_eval_score_v3 as v3

R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results")


def _cases():
    cases = {}
    for builder in (agent_eval_cases.build_cases, hc.build_heldout_cases, hc.build_heldout2_cases, hc.build_heldout3_cases,
                    hc.build_heldout4_cases, lc.build_large_cases, gc.build_gen_cases, sc.build_stats_cases):
        for c in builder():
            cases[c["id"] if "id" in c else c["case_id"]] = c
    return cases


def rescore3(run_dir):
    cases = _cases()
    known = {"demo": v2.db_amounts(agent_eval_cases.DEFAULT_DB), "L": v2.db_amounts(lc.LARGE_DB), "G": v2.db_amounts(gc.GEN_DB)}
    rows = []
    for fn in sorted(os.listdir(os.path.join(R, run_dir))):
        if not fn.startswith("case_"):
            continue
        f = json.load(open(os.path.join(R, run_dir, fn), encoding="utf-8"))
        cid = f["case_id"]; c = cases[cid]; ans = f.get("final_report") or ""
        k = known["G"] if cid.startswith(("G-", "S-")) else known["L"] if cid.startswith("L-") else known["demo"]
        unchanged = f.get("db_sha256_unchanged", False)
        rows.append({"id": cid, "cat": f["category"], "v1": bool(v1.score_case(c, ans)), "v2": bool(v2.score_case_v2(c, ans, unchanged, k)),
                     "v3": bool(v3.score_case_v3(c, ans, unchanged, k))})
    return rows


if __name__ == "__main__":
    for d in sys.argv[1:]:
        r = rescore3(d)
        print(f"{d:22s} n={len(r):3d}  v1={sum(x['v1'] for x in r):3d}  v2={sum(x['v2'] for x in r):3d}  v3={sum(x['v3'] for x in r):3d}")
