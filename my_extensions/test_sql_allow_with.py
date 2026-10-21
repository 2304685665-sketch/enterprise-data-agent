"""ODR_SQL_ALLOW_WITH：默认关保持旧行为；开启后合法 CTE 可用，WITH 包装的写操作仍被数据库层拒绝且库不变（每个开关值一个子进程）。"""
import hashlib, os, shutil, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); SRC = os.path.join(HERE, "..", "src")
CODE = r'''
import asyncio, os, sys, hashlib
sys.path.insert(0, {src!r}); os.environ.setdefault("OPENAI_API_KEY", "x")
from pathlib import Path
from open_deep_research import sql_executor as se
se.DB_PATH = Path({db!r})
h = lambda: hashlib.sha256(open({db!r}, "rb").read()).hexdigest()
b = h(); run = lambda q: asyncio.run(se.sql_executor.ainvoke({{"query": q}}))
print("CTE_SELECT=" + run("WITH t AS (SELECT 1 AS x) SELECT x FROM t").replace("\n", "|"))
for q in ("WITH c AS (SELECT 1) DELETE FROM companies", "WITH c AS (SELECT 1) INSERT INTO companies(name, industry, founded_year) VALUES ('E','x',2026)", "WITH c AS (SELECT 1) UPDATE companies SET name='H'", "WITH c AS (SELECT 1) DROP TABLE companies"):
    print("W=" + run(q)[:70].replace("\n", " "))
print("UNCHANGED=" + str(h() == b))
'''


def run(flag):
    d = tempfile.mkdtemp(); db = os.path.join(d, "demo.db"); shutil.copyfile(os.path.join(HERE, "demo.db"), db)
    env = {**os.environ, "ODR_SQL_ALLOW_WITH": flag}
    p = subprocess.run([sys.executable, "-c", CODE.format(src=SRC, db=db)], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr[-400:]
    return p.stdout


def test_default_off_blocks_with():
    o = run("0"); assert "CTE_SELECT=SQL Error: WITH (CTE)" in o and "UNCHANGED=True" in o; print("✅ test_default_off_blocks_with")


def test_on_allows_cte_select_and_db_rejects_writes():
    o = run("1"); assert "CTE_SELECT=x|1" in o and "UNCHANGED=True" in o
    ws = [l for l in o.splitlines() if l.startswith("W=")]
    assert len(ws) == 4 and all("readonly" in l.lower() or "read-only" in l.lower() or "SQL Error" in l for l in ws) and not any("successfully" in l for l in ws), ws
    print("✅ test_on_allows_cte_select_and_db_rejects_writes", ws[0])


if __name__ == "__main__":
    test_default_off_blocks_with(); test_on_allows_cte_select_and_db_rejects_writes(); print("全部测试通过")
