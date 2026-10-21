"""SQL 守卫误拦评测（PROTOCOL_sql_benign.md）：对冻结语料里每条合法只读查询，用真实 sql_executor 工具入口执行，
在数据库副本上进行。判定：
  blocked  = 返回以 "SQL Error: only SELECT" 或 "SQL Error: WITH (CTE)" 开头（守卫主动拒绝）
  error    = 其他 "SQL Error"（语法/不存在的列等，不是守卫的拒绝，单独计数）
  ok       = 其余
用法: [ODR_SQL_ALLOW_WITH=1] python eval_sql_benign.py [--out PATH]"""
import asyncio, json, os, shutil, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-eval")
from open_deep_research import sql_executor as se
import sql_benign_corpus as C
from pathlib import Path

EXPECTED_SHA = "9cda9463456725141f4dd6b0e0c1353775c598383766b0b507a45c43ec488f81"  # 冻结值（PROTOCOL_sql_benign.md）


def run(cases):
    tmp = tempfile.mkdtemp(); res = []
    try:
        for c in cases:
            dst = os.path.join(tmp, c["db"] + ".db"); shutil.copyfile(C.DBS[c["db"]], dst); se.DB_PATH = Path(dst)
            out = asyncio.run(se.sql_executor.ainvoke({"query": c["sql"]}))
            st = "blocked" if out.startswith(("SQL Error: only SELECT", "SQL Error: WITH (CTE)")) else "error" if out.startswith("SQL Error") else "ok"
            res.append({**c, "status": st, "out_head": out[:120]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


if __name__ == "__main__":
    sha = C.corpus_sha()
    if EXPECTED_SHA and sha != EXPECTED_SHA: raise SystemExit(f"语料 sha 与冻结值不符: {sha}")
    res = run(C.build()); from collections import Counter
    summ = {"corpus_sha256": sha, "allow_with": os.environ.get("ODR_SQL_ALLOW_WITH") == "1", "n": len(res),
            "by_kind": {k: dict(Counter(r["status"] for r in res if r["kind"] == k)) for k in sorted({r["kind"] for r in res})}}
    main = [r for r in res if r["kind"] != "non_select_readonly"]
    summ["main_n"] = len(main); summ["main_blocked"] = sum(r["status"] == "blocked" for r in main); summ["main_error"] = sum(r["status"] == "error" for r in main)
    summ["blocked_ids"] = [r["id"] for r in res if r["status"] == "blocked"]; summ["error_examples"] = [(r["id"], r["out_head"]) for r in res if r["status"] == "error"][:5]
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump({"summary": summ, "results": res}, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
