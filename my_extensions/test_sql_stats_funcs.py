"""ODR_SQL_STATS_FUNCS：关闭时 STDDEV 等函数不存在（与旧行为一致）；开启时在只读连接内对全部行计算，结果与 statistics 模块一致，
且不削弱只读保护。通过子进程切换环境变量（开关在 import 时读取）。"""
import json, os, statistics, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
LARGE = os.path.join(HERE, "eval_large.db")


def run(flag, query, db=LARGE):
    code = (
        "import sys,json,os;os.environ.setdefault('OPENAI_API_KEY','fake');"
        f"sys.path.insert(0,{SRC!r});from open_deep_research import sql_executor as se;"
        f"print(json.dumps({{'out':se._run_query_sync({query!r}),'desc':se.SQL_EXECUTOR_DESCRIPTION}}))"
    )
    env = dict(os.environ, ODR_SQL_DB_PATH=db)
    env.pop("ODR_SQL_STATS_FUNCS", None)
    if flag:
        env["ODR_SQL_STATS_FUNCS"] = "1"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr[-500:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def amounts():
    import sqlite3
    con = sqlite3.connect(f"file:{LARGE}?mode=ro", uri=True)
    return [r[0] for r in con.execute("select amount_usd from funding_rounds where amount_usd is not null")]


def first_number(out):
    return float(out.strip().splitlines()[-1].split()[-1])


class StatsFuncs(unittest.TestCase):
    def test_off_has_no_stddev_and_description_unchanged(self):
        r = run(False, "SELECT STDDEV(amount_usd) FROM funding_rounds")
        self.assertIn("no such function", r["out"])
        self.assertNotIn("STDDEV_POP", r["desc"])

    def test_on_matches_statistics_module_on_all_rows(self):
        a = amounts()
        for q, want in (("SELECT STDDEV_POP(amount_usd) FROM funding_rounds", statistics.pstdev(a)),
                        ("SELECT STDDEV_SAMP(amount_usd) FROM funding_rounds", statistics.stdev(a)),
                        ("SELECT STDDEV(amount_usd) FROM funding_rounds", statistics.stdev(a)),
                        ("SELECT VAR_POP(amount_usd) FROM funding_rounds", statistics.pvariance(a)),
                        ("SELECT MEDIAN(amount_usd) FROM funding_rounds", statistics.median(a))):
            r = run(True, q)
            self.assertAlmostEqual(first_number(r["out"]) / want, 1.0, places=9, msg=q)
        self.assertIn("STDDEV_POP", r["desc"])

    def test_group_by_and_empty_and_null(self):
        r = run(True, "SELECT MEDIAN(amount_usd) FROM funding_rounds WHERE 1=0")
        self.assertIn("None", r["out"])
        r = run(True, "SELECT round_type, COUNT(*), MEDIAN(amount_usd) FROM funding_rounds GROUP BY round_type")
        self.assertNotIn("Error", r["out"])

    def test_readonly_protections_still_hold_when_on(self):
        for q in ("DELETE FROM funding_rounds", "INSERT INTO companies(name) VALUES('x')", "ATTACH DATABASE 'x.db' AS x"):
            r = run(True, q)
            self.assertTrue(("Error" in r["out"]) or ("not allowed" in r["out"].lower()) or ("reject" in r["out"].lower()), q + " -> " + r["out"][:120])

    def test_nulls_are_ignored_and_single_value_sample_stddev_is_null(self):
        import sqlite3, tempfile
        d = tempfile.mkdtemp()
        db = os.path.join(d, "t.db")
        c = sqlite3.connect(db)
        c.execute("create table t(x real)")
        c.executemany("insert into t values (?)", [(1.0,), (None,), (3.0,), (None,), (5.0,)])
        c.commit(); c.close()
        r = run(True, "SELECT MEDIAN(x), STDDEV_POP(x), COUNT(x) FROM t", db=db)
        self.assertIn("3.0", r["out"])
        self.assertAlmostEqual(statistics.pstdev([1.0, 3.0, 5.0]), 1.6329931618554521, places=9)
        self.assertIn("1.63299316", r["out"])
        c = sqlite3.connect(db); c.execute("delete from t where x is null or x<>5"); c.commit(); c.close()
        r = run(True, "SELECT STDDEV_SAMP(x) FROM t", db=db)
        self.assertIn("None", r["out"])


if __name__ == "__main__":
    unittest.main()
