"""T1 SQL 攻击矩阵执行器——对新旧两个版本的 sql_executor 分别跑一遍
sql_guard_cases.py 里的全部用例，给出判定依据。

2026-10 修订（按审查意见）：
- A/N/R 三组分开统计，判定枚举从4类扩到5类，新增"测试未能验证"。
- PRAGMA 类用例（A15/A16/A23）新增"pragma探测"：除了 SHA-256，额外在
  运行前后分别用独立连接查询同一个 PRAGMA 的实际值，而不是只看文件哈希
  有没有变——user_version 这类会立刻持久化到文件头，journal_mode 从
  delete 切到 wal 通常会立刻生效但不一定马上改文件内容本身的哈希（取决于
  SQLite 内部何时真正写入），单靠 SHA-256 不够精确。
- A17（writable_schema）保留但不再参与自动判定为"拦截"，固定标记为
  "测试未能验证"并说明方法论局限（连接级设置，单语句单连接测不出来）。
- R01/R02（资源耗尽）引入"资源未受限"这个独立标签，和"攻击成功"
  （数据被真实篡改/新增可疑文件）区分开——完整吐出笛卡尔积不是数据被破坏，
  是没有资源限制，这是两件不同性质的事。

用法：
    .venv/bin/python3 eval_sql_guard.py --target <src目录路径> --label <版本标签>
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from sql_guard_cases import ATTACK_CASES, NORMAL_CASES, RESOURCE_CASES  # noqa: E402

REAL_DB = os.path.join(os.path.dirname(__file__), "demo.db")
PER_CASE_TIMEOUT = 30
SQLITE_AUX_SUFFIXES = ("-journal", "-wal", "-shm")

RUNNER_SCRIPT = """
import sys, asyncio, json
sys.path.insert(0, {target_src!r})
from pathlib import Path
from open_deep_research import sql_executor as mod

db_path = Path({db_path!r})
mod.DB_PATH = db_path

async def _run():
    try:
        result = await mod.sql_executor.ainvoke({{"query": {query!r}}})
        return {{"ok": True, "result": result}}
    except Exception as e:
        return {{"ok": False, "exception": f"{{type(e).__name__}}: {{e}}"}}

out = asyncio.run(_run())
print(json.dumps(out))
"""


def _sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity_check(path):
    try:
        conn = sqlite3.connect(path, timeout=5)
        cur = conn.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        conn.close()
        return rows == [("ok",)], str(rows)
    except Exception as e:
        return False, f"integrity_check自身失败: {type(e).__name__}: {e}"


def _probe_pragma(db_path, pragma_name):
    """独立连接查询一次指定PRAGMA的真实当前值。失败时如实返回错误描述，
    不伪装成某个具体值。"""
    if not os.path.exists(db_path):
        return "<db文件不存在>"
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        row = conn.execute(f"PRAGMA {pragma_name}").fetchone()
        conn.close()
        return row[0] if row else "<无返回值>"
    except Exception as e:
        return f"<探测失败: {type(e).__name__}: {e}>"


def _run_one_case(target_src, query, case_id, pragma_probe=None):
    tmpdir = tempfile.mkdtemp(prefix=f"sqlguard_{case_id}_")
    db_path = os.path.join(tmpdir, "demo.db")
    shutil.copy2(REAL_DB, db_path)

    sha_before = _sha256(db_path)
    files_before = set(os.listdir(tmpdir))
    pragma_before = _probe_pragma(db_path, pragma_probe) if pragma_probe else None

    script = RUNNER_SCRIPT.format(target_src=target_src, db_path=db_path, query=query)

    start = time.monotonic()
    infra_failure = None
    tool_output = None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=PER_CASE_TIMEOUT,
            cwd=tmpdir,  # 2026-10 修复：相对路径攻击（比如 ATTACH 'x.db'）之前会
            # 相对于父进程cwd解析，不是相对于这个用例的隔离临时目录——已经用
            # 真实复现证实过这个泄漏（new_evil.db 跑到了项目根目录），这里
            # 把子进程cwd显式锁定到隔离目录，堵住这个口子。
        )
        elapsed = time.monotonic() - start
        if proc.returncode != 0:
            infra_failure = f"子进程非零退出({proc.returncode}): stderr尾部: {proc.stderr[-500:]}"
        else:
            try:
                tool_output = json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception as e:
                infra_failure = f"子进程输出无法解析为JSON: {e}; stdout尾部: {proc.stdout[-300:]}"
    except subprocess.TimeoutExpired:
        elapsed = PER_CASE_TIMEOUT
        infra_failure = f"子进程超过{PER_CASE_TIMEOUT}秒父进程超时，已强制终止"

    sha_after = _sha256(db_path)
    files_after = set(os.listdir(tmpdir))
    new_files_all = files_after - files_before
    new_files_aux = {f for f in new_files_all if f.startswith("demo.db") and f.endswith(SQLITE_AUX_SUFFIXES)}
    new_files_suspicious = new_files_all - new_files_aux - {"demo.db"}

    integrity_ok, integrity_detail = (None, "数据库文件缺失，未检查") if not os.path.exists(db_path) else _integrity_check(db_path)
    pragma_after = _probe_pragma(db_path, pragma_probe) if pragma_probe else None

    shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "elapsed_s": round(elapsed, 2),
        "infra_failure": infra_failure,
        "tool_output": tool_output,
        "sha_before": sha_before,
        "sha_after": sha_after,
        "db_changed": sha_before != sha_after,
        "new_files_aux": sorted(new_files_aux),
        "new_files_suspicious": sorted(new_files_suspicious),
        "integrity_ok": integrity_ok,
        "integrity_detail": integrity_detail,
        "pragma_probe_name": pragma_probe,
        "pragma_before": pragma_before,
        "pragma_after": pragma_after,
    }


def _classify_attack(case, evidence):
    if evidence["infra_failure"]:
        return "测试基础设施失败"

    # A17：方法论本身覆盖不到，固定标记，不依赖任何自动判定
    if case.get("pragma_probe") is None and case["id"] == "A17":
        return "测试未能验证"

    attack_succeeded = (
        evidence["new_files_suspicious"]
        or evidence["integrity_ok"] is False
    )

    pragma_probe = case.get("pragma_probe")
    if pragma_probe:
        before = evidence["pragma_before"]
        after = evidence["pragma_after"]
        target = case.get("pragma_target")
        # 字符串类pragma统一转小写比较，数值类保持原样
        norm = lambda v: v.lower() if isinstance(v, str) else v
        if norm(before) == norm(target):
            # 目标值和原值相同，这条用例本身是no-op，测不出拦截/成功
            return "测试未能验证"
        if norm(after) == norm(target):
            # 真实写入生效了
            return "攻击成功"
        if norm(after) == norm(before):
            # 值没变，且原值不等于目标值——这是真拦截
            return "攻击被拦截"
        # after 既不等于 before 也不等于 target，出现了预期之外的第三个值
        return "测试未能验证"

    if attack_succeeded:
        return "攻击成功"
    return "攻击被拦截" if not evidence["db_changed"] else "测试未能验证"


def _classify_normal(evidence):
    if evidence["infra_failure"]:
        return "测试基础设施失败"
    out = evidence["tool_output"]
    if out is None:
        return "测试基础设施失败"
    if not out["ok"]:
        return "正常查询被误拦"
    content = out["result"]
    looks_like_error = isinstance(content, str) and content.startswith(
        ("SQL Error:", "Error executing tool:")
    )
    return "正常查询被误拦" if looks_like_error else "正常查询通过"


def _classify_resource(evidence):
    if evidence["infra_failure"]:
        return "测试基础设施失败"
    if evidence["new_files_suspicious"] or evidence["integrity_ok"] is False:
        return "攻击成功"
    result_text = str(evidence["tool_output"].get("result", "")) if evidence["tool_output"] else ""
    if "truncated" in result_text.lower() or "timed out" in result_text.lower():
        return "攻击被拦截"
    # 没有截断/超时提示，也没有数据被篡改——说明请求被完整执行了，
    # 没有任何行数/时间上限介入，这是"资源未受限"，不是"攻击成功"
    # （没有数据完整性被破坏，只是没有限制）。
    return "资源未受限"


def run_matrix(target_src, label):
    results = []
    for case in ATTACK_CASES:
        ev = _run_one_case(target_src, case["sql"], case["id"], pragma_probe=case.get("pragma_probe"))
        verdict = _classify_attack(case, ev)
        results.append({"id": case["id"], "kind": "attack", "category": case["category"],
                         "sql": case["sql"], "verdict": verdict, "evidence": ev})
    for case in NORMAL_CASES:
        ev = _run_one_case(target_src, case["sql"], case["id"])
        verdict = _classify_normal(ev)
        results.append({"id": case["id"], "kind": "normal", "category": "正常查询",
                         "sql": case["sql"], "verdict": verdict, "evidence": ev})
    for case in RESOURCE_CASES:
        ev = _run_one_case(target_src, case["sql"], case["id"])
        verdict = _classify_resource(ev)
        results.append({"id": case["id"], "kind": "resource", "category": "资源耗尽",
                         "sql": case["sql"], "verdict": verdict, "evidence": ev})

    print(f"\n{'='*20} {label} ({target_src}) {'='*20}")
    for r in results:
        print(f"[{r['id']}] {r['verdict']:12s} ({r['category']}) elapsed={r['evidence']['elapsed_s']}s")

    # 分组统计，三组互不混算，每组加总必须等于该组用例总数
    for kind, total in (("attack", len(ATTACK_CASES)), ("normal", len(NORMAL_CASES)), ("resource", len(RESOURCE_CASES))):
        group = [r for r in results if r["kind"] == kind]
        counts = {}
        for r in group:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print(f"\n--- {kind} 组统计（共{total}条） ---")
        for k, v in counts.items():
            print(f"  {k}: {v}")
        assert sum(counts.values()) == total == len(group), f"{kind}组统计没有闭合！"

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    results = run_matrix(args.target, args.label)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n结果已写入 {args.out}")
