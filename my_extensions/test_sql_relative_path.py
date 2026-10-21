"""ODR_SQL_DB_PATH 给相对路径时不能让异常逃出工具（此前 Path.as_uri() 抛 ValueError → infrastructure_error），
也不能在 ODR_SQL_SCHEMA_HINT=1 时让模块导入失败。用法：PYTHONPATH=src python my_extensions/test_sql_relative_path.py"""
import os, sqlite3, subprocess, sys, tempfile
from pathlib import Path

CODE = ("import asyncio\nfrom open_deep_research import sql_executor as m\n"
        "print(asyncio.run(m.sql_executor.ainvoke({'query':'select count(*) from t'})))")

def run(extra_env):
    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "sub").mkdir()
        c = sqlite3.connect(Path(t) / "sub" / "x.db"); c.execute("create table t(a)"); c.execute("insert into t values (1)"); c.commit(); c.close()
        env = {k: v for k, v in os.environ.items() if not k.startswith("ODR_")}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")   # 子进程换了工作目录，必须用绝对路径
        env.update(extra_env); env["ODR_SQL_DB_PATH"] = "sub/x.db"      # 相对路径，工作目录 = 临时目录
        return subprocess.run([sys.executable, "-c", CODE], env=env, cwd=t, capture_output=True, text=True)

def test_relative_path_works():
    r = run({}); assert r.returncode == 0 and "1" in r.stdout and "ValueError" not in r.stderr, r.stderr[-300:]
    print("✅ 相对路径可用（不再抛 ValueError）")

def test_relative_path_with_schema_hint():
    r = run({"ODR_SQL_SCHEMA_HINT": "1"}); assert r.returncode == 0 and "1" in r.stdout, r.stderr[-300:]
    print("✅ 相对路径 + SCHEMA_HINT 可导入且可查询")

test_relative_path_works(); test_relative_path_with_schema_hint()
