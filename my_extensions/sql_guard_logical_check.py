"""SQL 守卫“逻辑变更”判据（独立于 eval_sql_guard 的分类）：对每条攻击语句，在 demo.db 副本上执行，比较执行前后的
(1) 完整 SQL dump（含 schema、触发器、视图、索引、全部数据）；(2) user_version / application_id / journal_mode；(3) 是否出现数据库及其辅助文件以外的新文件。
任一不同即记为“逻辑上被改动”。动机：eval_sql_guard 把“哈希变了但无可疑新文件/完整性损坏”标成“测试未能验证”，这对有防护版本是保守的，
但会把无防护基线里明显的 DELETE/DROP 也标成“未能验证”，使对比失真。本脚本不替代原分类，只补充语义判据。
用法: python sql_guard_logical_check.py --target <含 open_deep_research/sql_executor.py 的 src 目录> --label X --out PATH"""
import argparse, json, os, shutil, sqlite3, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from sql_guard_cases import ATTACK_CASES  # noqa: E402
from eval_sql_guard import RUNNER_SCRIPT, REAL_DB, SQLITE_AUX_SUFFIXES  # noqa: E402


def snapshot(db):
    c = sqlite3.connect(db)
    try:
        dump = "\n".join(c.iterdump())
        pr = {p: c.execute(f"PRAGMA {p}").fetchone()[0] for p in ("user_version", "application_id", "journal_mode")}
    finally:
        c.close()
    return dump, pr


def run_case(target, case):
    d = tempfile.mkdtemp(prefix="lc_")
    db = os.path.join(d, "demo.db"); shutil.copy2(REAL_DB, db)
    before_dump, before_pr = snapshot(db)
    files_before = set(os.listdir(d))
    script = RUNNER_SCRIPT.format(target_src=target, db_path=db, query=case["sql"])
    try:
        p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, cwd=d)
        infra = p.stderr[-200:] if p.returncode else None
    except subprocess.TimeoutExpired:
        infra = "timeout"
    try:
        after_dump, after_pr = snapshot(db)
    except Exception as e:  # 数据库被破坏到无法打开，也算被改动
        after_dump, after_pr = f"UNREADABLE:{e}", {}
    new_files = {f for f in set(os.listdir(d)) - files_before if not f.endswith(SQLITE_AUX_SUFFIXES)}
    changed = (after_dump != before_dump) or (after_pr != before_pr) or bool(new_files)
    shutil.rmtree(d, ignore_errors=True)
    return {"id": case["id"], "logical_change": changed, "dump_changed": after_dump != before_dump,
            "pragma_changed": after_pr != before_pr, "new_files": sorted(new_files), "infra_failure": infra}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--target", required=True); ap.add_argument("--label", required=True); ap.add_argument("--out")
    a = ap.parse_args()
    rows = [run_case(a.target, c) for c in ATTACK_CASES]
    n = sum(r["logical_change"] for r in rows)
    print(f"{a.label}: 攻击语句 {len(rows)} 条，逻辑上改动了数据库 {n} 条；基础设施失败 {sum(bool(r['infra_failure']) for r in rows)} 条")
    print("改动的:", [r["id"] for r in rows if r["logical_change"]])
    if a.out:
        if os.path.exists(a.out): raise SystemExit(f"拒绝覆盖: {a.out}")
        json.dump({"label": a.label, "n": len(rows), "changed": n, "rows": rows}, open(a.out, "w"), ensure_ascii=False, indent=1)
