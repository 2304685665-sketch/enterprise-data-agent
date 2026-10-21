"""ODR_SQL_METRIC_DICT_PATH 开关：默认关闭时工具说明不变；设置后追加字典；文件不存在时静默回退。
用法：PYTHONPATH=src python my_extensions/test_sql_metric_dict.py"""
import os, subprocess, sys, tempfile
from pathlib import Path

CODE = "from open_deep_research.sql_executor import SQL_EXECUTOR_DESCRIPTION as d; print(len(d)); print('Business definitions' in d)"

def desc_info(env_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ODR_")}
    if env_path is not None:
        env["ODR_SQL_METRIC_DICT_PATH"] = env_path
    out = subprocess.run([sys.executable, "-c", CODE], env=env, capture_output=True, text=True, check=True).stdout.split()
    return int(out[0]), out[1] == "True"

def main():
    base_len, base_has = desc_info(None)
    assert not base_has
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "d.md"; f.write_text("Business definitions: test", encoding="utf-8")
        n, has = desc_info(str(f))
        assert has and n > base_len
    n, has = desc_info(str(Path(t) / "missing.md"))   # 目录已删除 → 文件不存在
    assert not has and n == base_len
    print("✅ test_sql_metric_dict 通过（默认不变 / 开启追加 / 缺文件回退）")

def empty_msg(flag):
    code = ("import asyncio,os,sqlite3,tempfile,pathlib;d=pathlib.Path(tempfile.mkdtemp())/'t.db';c=sqlite3.connect(d);c.execute('create table x(a)');c.commit();c.close();"
            "os.environ['ODR_SQL_DB_PATH']=str(d);from open_deep_research import sql_executor as m;"
            "print(asyncio.run(m.sql_executor.ainvoke({'query':'select * from x'})))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ODR_")}
    if flag: env["ODR_SQL_EMPTY_HINT"] = "1"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout

def agg_msg(flag):
    code = ("import asyncio,os,sqlite3,tempfile,pathlib;d=pathlib.Path(tempfile.mkdtemp())/'t.db';c=sqlite3.connect(d);c.execute('create table x(a,n)');c.execute(\"insert into x values ('客户C',5)\");c.commit();c.close();"
            "os.environ['ODR_SQL_DB_PATH']=str(d);from open_deep_research import sql_executor as m;"
            "q=lambda s: asyncio.run(m.sql_executor.ainvoke({'query':s}));"
            "print(q(\"select count(*), sum(n) from x where a='customer C'\"));print('---');print(q(\"select count(*), sum(n) from x where a='客户C'\"))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ODR_")}
    if flag: env["ODR_SQL_EMPTY_HINT"] = "1"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.split("---")

def test_aggregate_zero_hint():
    off, on = agg_msg(False), agg_msg(True)
    assert "NOTE" not in off[0] and "NOTE" not in off[1]
    assert "SELECT DISTINCT" in on[0] and "NOTE" not in on[1]      # 0/NULL 触发；正常结果 (1 | 5) 不触发
    print("✅ test_aggregate_zero_hint 通过（0/NULL 聚合触发提示，正常结果不触发）")

def test_empty_hint():
    off, on = empty_msg(False), empty_msg(True)
    assert off.strip() == "Query executed successfully but returned no rows."
    assert on.startswith("Query executed successfully but returned no rows.") and "SELECT DISTINCT" in on
    print("✅ test_empty_hint 通过（默认不变 / 开启后提示先 DISTINCT）")

main()
test_empty_hint()
test_aggregate_zero_hint()
