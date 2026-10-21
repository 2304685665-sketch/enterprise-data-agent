"""用评分器 v2 对已有运行目录离线重评（不调用任何 API、不改动运行目录）。
用法: python agent_eval_rescore_v2.py <运行目录名> [<运行目录名> ...]"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_eval_cases_stats as sc, agent_eval_cases, agent_eval_cases_heldout as hc, agent_eval_cases_large as lc, agent_eval_cases_gen as gc, agent_eval_score as v1, agent_eval_score_v2 as v2

R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results")


def rescore(run_dir):
    # 各套件的用例 id 前缀互不相同（无前缀=main，h-/h2-/h3-=留出集，L-=main2/eval_large.db）
    cases = {}
    for builder in (agent_eval_cases.build_cases, hc.build_heldout_cases, hc.build_heldout2_cases,
                    hc.build_heldout3_cases, hc.build_heldout4_cases, lc.build_large_cases, gc.build_gen_cases, sc.build_stats_cases):
        for c in builder():
            cases[c["id"] if "id" in c else c["case_id"]] = c
    known_demo = v2.db_amounts(agent_eval_cases.DEFAULT_DB)
    known_large = v2.db_amounts(lc.LARGE_DB)
    known_gen = v2.db_amounts(gc.GEN_DB)
    rows = []
    for fn in sorted(os.listdir(os.path.join(R, run_dir))):
        if not fn.startswith("case_"):
            continue
        f = json.load(open(os.path.join(R, run_dir, fn), encoding="utf-8"))
        c = cases[f["case_id"]]
        ans = f.get("final_report") or ""
        a1 = bool(v1.score_case(c, ans)); a2 = v2.score_case_v2(c, ans, f.get("db_sha256_unchanged", False), known_gen if f["case_id"].startswith(("G-", "S-")) else known_large if f["case_id"].startswith("L-") else known_demo)
        rows.append({"id": f["case_id"], "cat": f["category"], "kind": c["expected"]["kind"], "recorded_v1": f.get("score_passed"), "v1": a1, "v2": a2})
    return rows


if __name__ == "__main__":
    for d in sys.argv[1:]:
        rows = rescore(d)
        bad = [r["id"] for r in rows if r["recorded_v1"] is not None and r["recorded_v1"] != r["v1"]]
        print(d, "n=", len(rows), "v1重算与记录不一致:", bad, "| v1通过", sum(r["v1"] for r in rows), "v2通过", sum(r["v2"] for r in rows))
        for r in rows:
            if r["v1"] != r["v2"]:
                print("  变化", r["id"], r["kind"], "v1", r["v1"], "->v2", r["v2"])
        json.dump(rows, open(os.path.join(R, d.replace("agent_eval_run_", "rescore_v2_") + ".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
