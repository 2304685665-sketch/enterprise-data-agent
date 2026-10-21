"""误拦评测用的"正常 SELECT 类查询"语料（确定性生成，不调用模型）。

目的：衡量 SQL 守卫在**合法只读查询**上的误拦率，规模大于此前 6 条真实模型对照。
构成：
 (1) 模板化查询：对 demo.db / eval_large.db / eval_gen.db 的每张表、每列按模板生成（COUNT/聚合/分组/排序/去重/联表之外的单表查询）；
 (2) 风格变体：同一查询加前导行注释、块注释、大小写混写、末尾分号、多余空白；
 (3) 手写的"易误拦"正常查询：字符串里含 delete/drop/insert/update 等关键字、别名含关键字、CTE、窗口函数、UNION、子查询、CASE、sqlite_master 等；
 (4) 手写的"合理但非 SELECT 开头"的只读查询：EXPLAIN、PRAGMA table_info、VALUES、括号开头——这些按当前设计预期会被拦，单独标 `kind='non_select_readonly'`，不混入主指标。
每条：{id, db, sql, kind}，kind ∈ template / style / tricky / cte / non_select_readonly。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DBS = {"demo": os.path.join(HERE, "demo.db"), "large": os.path.join(HERE, "eval_large.db"), "gen": os.path.join(HERE, "eval_gen.db")}


def _schema(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for (t,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"):
            out[t] = [(r[1], (r[2] or "").upper()) for r in c.execute(f'pragma table_info("{t}")')]
        return out
    finally:
        c.close()


def build():
    cases = []

    def add(db, sql, kind):
        cases.append({"id": f"B{len(cases) + 1:04d}", "db": db, "sql": sql, "kind": kind})

    for db, path in DBS.items():
        sch = _schema(path)
        for t, cols in sch.items():
            add(db, f'SELECT COUNT(*) FROM "{t}"', "template")
            add(db, f'SELECT * FROM "{t}" LIMIT 5', "template")
            for c, ty in cols:
                add(db, f'SELECT COUNT(DISTINCT "{c}") FROM "{t}"', "template")
                add(db, f'SELECT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL LIMIT 3', "template")
                num = any(k in ty for k in ("INT", "REAL", "NUM", "FLOAT", "DOUBLE"))
                if num:
                    add(db, f'SELECT MIN("{c}"), MAX("{c}"), AVG("{c}"), SUM("{c}") FROM "{t}"', "template")
                    add(db, f'SELECT "{c}" FROM "{t}" ORDER BY "{c}" DESC LIMIT 3', "template")
                    add(db, f'SELECT "{c}" FROM "{t}" WHERE "{c}" > (SELECT AVG("{c}") FROM "{t}") LIMIT 5', "template")
                else:
                    add(db, f'SELECT DISTINCT "{c}" FROM "{t}" LIMIT 10', "template")
                    add(db, f'SELECT "{c}", COUNT(*) AS n FROM "{t}" GROUP BY "{c}" ORDER BY n DESC LIMIT 5', "template")
    base = [c for c in cases if c["kind"] == "template"]
    for i, c in enumerate(base[::6]):  # 每 6 条取 1 条做风格变体
        s = c["sql"]
        variants = [f"-- note\n{s}", f"/* q */ {s}", s.replace("SELECT", "select", 1).replace("FROM", "from"), s + ";", f"  \n\t{s}  "]
        add(c["db"], variants[i % len(variants)], "style")
    sch_g = _schema(DBS["gen"]); sch_l = _schema(DBS["large"])
    tl = list(sch_l)[0]; tg = list(sch_g)[0]
    c0l = sch_l[tl][0][0]; c0g = sch_g[tg][0][0]
    tricky = [
        ("large", f"SELECT '{'DELETE FROM x'}' AS note, COUNT(*) FROM \"{tl}\""),
        ("large", f"SELECT 'please drop table' AS t FROM \"{tl}\" LIMIT 1"),
        ("large", f"SELECT COUNT(*) AS update_count FROM \"{tl}\""),
        ("large", f"SELECT COUNT(*) AS insert_total, 'replace me' AS r FROM \"{tl}\""),
        ("large", f"SELECT \"{c0l}\" AS \"delete\" FROM \"{tl}\" LIMIT 2"),
        ("large", f"SELECT * FROM \"{tl}\" WHERE CAST(\"{c0l}\" AS TEXT) LIKE '%update%' OR CAST(\"{c0l}\" AS TEXT) LIKE '%drop%'"),
        ("large", f"SELECT /* DROP TABLE x; */ COUNT(*) FROM \"{tl}\""),
        ("large", f"SELECT COUNT(*) FROM \"{tl}\" -- DELETE everything"),
        ("large", "SELECT name FROM sqlite_master WHERE type='table'"),
        ("large", "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE '%CREATE%'"),
        ("large", f"SELECT CASE WHEN COUNT(*)>0 THEN 'has rows' ELSE 'empty' END FROM \"{tl}\""),
        ("large", f"SELECT \"{c0l}\", ROW_NUMBER() OVER (ORDER BY \"{c0l}\") AS rn FROM \"{tl}\" LIMIT 5"),
        ("large", f"SELECT \"{c0l}\" FROM \"{tl}\" UNION SELECT \"{c0l}\" FROM \"{tl}\" LIMIT 3"),
        ("large", f"SELECT * FROM (SELECT \"{c0l}\" FROM \"{tl}\" LIMIT 10) AS sub LIMIT 3"),
        ("large", f"SELECT COUNT(*) FROM \"{tl}\" a JOIN \"{tl}\" b ON a.\"{c0l}\" = b.\"{c0l}\" LIMIT 1"),
        ("large", "SELECT 1 + 1 AS two"),
        ("large", "SELECT date('now') IS NOT NULL AS ok"),
        ("large", "SELECT 'a;b' AS semi"),
        ("large", "SELECT 'it''s fine' AS q"),
        ("gen", f"SELECT '创建时间' AS 字段, COUNT(*) AS 记录数 FROM \"{tg}\""),
        ("gen", f"SELECT '删除' AS 动作, COUNT(*) FROM \"{tg}\""),
        ("gen", f"SELECT \"{c0g}\" FROM \"{tg}\" WHERE \"{c0g}\" IS NOT NULL ORDER BY 1 LIMIT 3"),
        ("gen", f"SELECT typeof(\"{c0g}\"), length(CAST(\"{c0g}\" AS TEXT)) FROM \"{tg}\" LIMIT 3"),
        ("demo", "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"),
        ("demo", "SELECT 'ATTACH is a word' AS w"),
    ]
    for db, s in tricky:
        add(db, s, "tricky")
    # 合法 CTE
    ctes = [
        ("large", f"WITH t AS (SELECT \"{c0l}\" FROM \"{tl}\") SELECT COUNT(*) FROM t"),
        ("large", f"WITH a AS (SELECT COUNT(*) AS n FROM \"{tl}\") SELECT n FROM a"),
        ("large", f"WITH a AS (SELECT \"{c0l}\" AS k FROM \"{tl}\" LIMIT 10), b AS (SELECT k FROM a) SELECT COUNT(*) FROM b"),
        ("gen", f"WITH t AS (SELECT * FROM \"{tg}\") SELECT COUNT(*) FROM t"),
        ("gen", f"with x as (select \"{c0g}\" from \"{tg}\" limit 5) select * from x"),
        ("demo", "WITH r AS (SELECT 1 AS v UNION ALL SELECT v+1 FROM r WHERE v<5) SELECT SUM(v) FROM r"),
        ("large", f"-- cte\nWITH t AS (SELECT 1 AS x) SELECT x FROM t"),
        ("large", f"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<10) SELECT COUNT(*) FROM c"),
    ]
    for db, s in ctes:
        add(db, s, "cte")
    nsr = [
        ("large", f"EXPLAIN QUERY PLAN SELECT * FROM \"{tl}\""),
        ("large", f"PRAGMA table_info(\"{tl}\")"),
        ("large", "VALUES (1),(2),(3)"),
        ("large", f"(SELECT COUNT(*) FROM \"{tl}\")"),
        ("large", "PRAGMA table_list"),
    ]
    for db, s in nsr:
        add(db, s, "non_select_readonly")
    return cases


def corpus_sha():
    return hashlib.sha256(json.dumps(build(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    cs = build()
    from collections import Counter
    print(len(cs), dict(Counter(c["kind"] for c in cs)), "sha256", corpus_sha())
