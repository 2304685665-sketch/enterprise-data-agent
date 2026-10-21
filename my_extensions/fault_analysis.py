"""端到端故障注入分析（PROTOCOL_fault_e2e.md）。拒绝含模型调用错误的运行。用法: python fault_analysis.py [--out PATH]"""
import glob, json, math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import agent_eval_cases_inject as IC
R = os.path.join(HERE, "eval_results")


def load(d):
    cases = {c["id"]: c for c in IC.build_inject_cases()}; rows = {}
    for f in glob.glob(os.path.join(R, d, "case_*.json")):
        j = json.load(open(f, encoding="utf-8")); mc = j.get("model_calls")
        if (isinstance(mc, dict) and mc.get("error", 0)) or "insufficient_quota" in json.dumps(j):
            raise SystemExit(f"{d}/{os.path.basename(f)} 含模型调用错误/额度耗尽，运行作废")
        ft = j.get("fault") or {}
        rows[j["case_id"]] = {"useful": cases[j["case_id"]]["expected"]["value"] in (j.get("final_report") or ""),
                              "attempts": ft.get("doc_search_attempts", 0), "injected": ft.get("injected", 0)}
    return rows


def mcnemar(a, b):
    n = a + b
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(min(a, b) + 1)) / 2 ** n)


if __name__ == "__main__":
    A, B = load("fault_F0"), load("fault_F1")
    if len(A) != 16 or len(B) != 16: raise SystemExit("未跑完 16 例")
    out = {}
    for n, r in (("F0", A), ("F1", B)):
        att = sum(x["attempts"] for x in r.values()); inj = sum(x["injected"] for x in r.values())
        out[n] = {"doc_search_attempts": att, "injected": inj, "attempt_success_rate": round(1 - inj / att, 3) if att else None,
                  "useful": sum(x["useful"] for x in r.values())}
    ids = sorted(A); a = sum(A[i]["useful"] and not B[i]["useful"] for i in ids); b = sum(B[i]["useful"] and not A[i]["useful"] for i in ids)
    out["mcnemar_useful"] = {"only_F0": a, "only_F1": b, "p": mcnemar(a, b)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(out, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
