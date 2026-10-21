"""规模化攻击评测：对一个 target（含 open_deep_research/sql_executor.py 的 src 目录）逐条求值，逻辑改动判据同 sql_guard_logical_check。
用法: [ODR_SQL_ALLOW_WITH=1] python sql_attack_scale.py --target ABS_SRC --label X --out PATH"""
import argparse, json, os, sys
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import sql_attack_corpus as C
from sql_guard_logical_check import run_case

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--target", required=True); ap.add_argument("--label", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not os.path.isabs(a.target): raise SystemExit("--target 必须是绝对路径（相对路径会让子进程 ModuleNotFoundError，结果无效）")
    if os.path.exists(a.out): raise SystemExit(f"拒绝覆盖: {a.out}")
    cases = C.build(); meta = {c["id"]: c for c in cases}
    with ThreadPoolExecutor(6) as ex: rows = list(ex.map(lambda c: run_case(a.target, c), cases))
    for r in rows: r.update(op=meta[r["id"]]["op"], transform=meta[r["id"]]["transform"])
    infra = [r["id"] for r in rows if r["infra_failure"]]
    changed = [r for r in rows if r["logical_change"]]
    summ = {"label": a.label, "corpus_sha256": C.corpus_sha(), "n": len(rows), "changed": len(changed), "infra_failures": len(infra),
            "changed_by_transform": {t: sum(r["transform"] == t for r in changed) for t in sorted({r["transform"] for r in rows})},
            "changed_by_op": {o: sum(r["op"] == o for r in changed) for o in sorted({r["op"] for r in rows})}}
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    json.dump({"summary": summ, "rows": rows}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if infra: print("基础设施失败 %d 条：本次结果无效" % len(infra), file=sys.stderr)
