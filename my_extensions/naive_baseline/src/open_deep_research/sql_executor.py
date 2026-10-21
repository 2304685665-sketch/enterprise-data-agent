"""无防护基线（评测专用，禁止用于生产）：一个“拿到 SQL 就执行”的朴素 SQL 工具。
特征：可写连接、不做语句分类/白名单、不限制行数和时间、执行后提交。接口与真实 sql_executor 一致
（模块级 DB_PATH 和带 ainvoke 的 sql_executor），用于 eval_sql_guard 的“无防护”基线臂。"""
import sqlite3
from pathlib import Path

DB_PATH = Path("demo.db")


class _Tool:
    async def ainvoke(self, args):
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cur = conn.cursor()
            cur.execute(args["query"])
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.commit()
            return " ".join(cols) + "\n" + "\n".join(" ".join(map(str, r)) for r in rows)
        finally:
            conn.close()


sql_executor = _Tool()
