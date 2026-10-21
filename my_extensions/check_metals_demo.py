"""核对 metals_demo.db 与 sql_executor（不调用任何大模型）。

做三件事：
1. 用独立的 Python 代码从原始表算出"标准答案"（不经过 SQL 聚合）。
2. 把参考 SQL 交给真实的 sql_executor 工具执行，逐项比对输出。
3. 验证写操作被拒绝、数据库未被改动、超出数据范围的查询返回空而不是编造。

用法（项目根目录）：
    uv run python my_extensions/check_metals_demo.py
只依赖 langchain-core；会先把数据库复制到临时目录再只读打开，不改动原文件。
"""
import asyncio
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "metals_demo.db"
TMP = Path(tempfile.mkdtemp())
DB = TMP / "metals_demo.db"
shutil.copyfile(SRC, DB)
os.environ["ODR_SQL_DB_PATH"] = str(DB)   # 必须在加载 sql_executor 之前设置

spec = importlib.util.spec_from_file_location("sql_executor_mod", HERE.parent / "src" / "open_deep_research" / "sql_executor.py")
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)

raw = sqlite3.connect(DB)
AS_OF = raw.execute("select value from meta where key='as_of'").fetchone()[0]
names = dict(raw.execute("select id,name from counterparties"))
trades = raw.execute("select id,trade_date,metal,direction,counterparty_id,qty_ton,unit_price_cny from trades").fetchall()
recs = raw.execute("select customer_id,amount_due_cny,amount_paid_cny,due_date from receivables").fetchall()
prices = raw.execute("select price_month,metal,cny_per_t from market_prices").fetchall()


# ---------- 标准答案：纯 Python，独立于参考 SQL ----------
def avg_buy(metal):
    b = [(t[5], t[6]) for t in trades if t[2] == metal and t[3] == "buy"]
    return sum(q * p for q, p in b) / sum(q for q, _ in b)

def gold_q1():
    out = {}
    for t in trades:
        if t[3] == "sell":
            out[t[2]] = out.get(t[2], 0) + t[5] * (t[6] - avg_buy(t[2]))
    return sorted(([m, v] for m, v in out.items()), key=lambda r: -r[1])

def gold_q2():
    out = {}
    for cid, due, paid, dd in recs:
        if dd < AS_OF and due > paid:
            out[names[cid]] = out.get(names[cid], 0) + due - paid
    return sorted(([n, v] for n, v in out.items()), key=lambda r: -r[1])

def gold_q3():
    d0 = date.fromisoformat(AS_OF)
    return sorted([n] for n in {names[c] for c, due, paid, dd in recs if due > paid and (d0 - date.fromisoformat(dd)).days > 90})

def gold_q4():
    latest = {m: p for ym, m, p in prices if ym == "2026-07"}
    return [[m, avg_buy(m), latest[m], latest[m] - avg_buy(m)] for m in sorted(latest)]

def gold_q5():
    cu = {ym: p for ym, m, p in prices if m == "copper"}
    recent = [p for ym, p in cu.items() if ym >= "2026-01"]
    earlier = [p for ym, p in cu.items() if "2025-08" <= ym <= "2025-12"]
    return [[sum(recent) / len(recent), sum(earlier) / len(earlier)]]

def gold_q6():
    out = {}
    for t in trades:
        if t[3] == "sell":
            out[t[1][:7]] = out.get(t[1][:7], 0) + t[5] * t[6]
    return [[m, v] for m, v in sorted(out.items(), key=lambda kv: -kv[1])[:3]]

def gold_q7():
    out, total = {}, 0
    for t in trades:
        if t[3] == "sell":
            v = t[5] * t[6]
            out[names[t[4]]] = out.get(names[t[4]], 0) + v
            total += v
    return sorted(([n, 100 * v / total] for n, v in out.items()), key=lambda r: -r[1])


CASES = [
    ("Q1", "各品种毛利排名（销售价减全期加权平均采购价）",
     "SELECT s.metal, ROUND(SUM(s.qty_ton*(s.unit_price_cny - b.avg_buy)),0) AS gross_cny FROM trades s "
     "JOIN (SELECT metal, SUM(qty_ton*unit_price_cny)/SUM(qty_ton) AS avg_buy FROM trades WHERE direction='buy' GROUP BY metal) b "
     "ON b.metal=s.metal WHERE s.direction='sell' GROUP BY s.metal ORDER BY gross_cny DESC", gold_q1, 1.0),
    ("Q2", "截至 as_of，各客户的逾期未收余额（从大到小）",
     "SELECT cp.name, ROUND(SUM(r.amount_due_cny-r.amount_paid_cny),0) AS overdue_cny FROM receivables r "
     "JOIN counterparties cp ON cp.id=r.customer_id WHERE r.due_date < (SELECT value FROM meta WHERE key='as_of') "
     "AND r.amount_due_cny > r.amount_paid_cny GROUP BY cp.name ORDER BY overdue_cny DESC", gold_q2, 1.0),
    ("Q3", "哪些客户有逾期超过 90 天的未收款",
     "SELECT DISTINCT cp.name FROM receivables r JOIN counterparties cp ON cp.id=r.customer_id "
     "WHERE r.amount_due_cny > r.amount_paid_cny AND julianday((SELECT value FROM meta WHERE key='as_of')) - julianday(r.due_date) > 90 "
     "ORDER BY cp.name", gold_q3, 0),
    ("Q4", "各品种库存成本与最新基准价（2026-07）的差价，元/吨",
     "SELECT i.metal, ROUND(i.avg_cost_cny,0), ROUND(m.cny_per_t,0), ROUND(m.cny_per_t - i.avg_cost_cny,0) FROM inventory i "
     "JOIN market_prices m ON m.metal=i.metal AND m.price_month='2026-07' ORDER BY i.metal", gold_q4, 1.5),
    ("Q5", "铜：2026 年 1-7 月均价与 2025 年 8-12 月均价（元/吨）",
     "SELECT ROUND(AVG(CASE WHEN price_month>='2026-01' THEN cny_per_t END),0) AS recent_avg, "
     "ROUND(AVG(CASE WHEN price_month BETWEEN '2025-08' AND '2025-12' THEN cny_per_t END),0) AS earlier_avg "
     "FROM market_prices WHERE metal='copper'", gold_q5, 1.0),
    ("Q6", "销售额最高的 3 个月",
     "SELECT substr(trade_date,1,7) AS month, ROUND(SUM(qty_ton*unit_price_cny),0) AS sales_cny FROM trades "
     "WHERE direction='sell' GROUP BY month ORDER BY sales_cny DESC LIMIT 3", gold_q6, 1.0),
    ("Q7", "各客户销售额占比（%）",
     "SELECT cp.name, ROUND(100.0*SUM(t.qty_ton*t.unit_price_cny)/(SELECT SUM(qty_ton*unit_price_cny) FROM trades WHERE direction='sell'),1) AS share_pct "
     "FROM trades t JOIN counterparties cp ON cp.id=t.counterparty_id WHERE t.direction='sell' GROUP BY cp.name ORDER BY share_pct DESC", gold_q7, 0.11),
]


def parse(out: str):
    lines = out.split("\n")
    return [l.split(" | ") for l in lines[1:]]

def same(got, gold, tol):
    if len(got) != len(gold):
        return False
    for g_row, e_row in zip(got, gold):
        if len(g_row) != len(e_row):
            return False
        for g, e in zip(g_row, e_row):
            try:
                if abs(float(g) - float(e)) > tol:
                    return False
            except (TypeError, ValueError):
                if str(g) != str(e):
                    return False
    return True


async def main() -> int:
    fails = 0
    print(f"数据库：{SRC.name}　as_of={AS_OF}　（真实公开行情 + 模拟台账，见 setup_metals_demo_db.py）\n")
    for qid, question, sql, gold_fn, tol in CASES:
        out = await se.sql_executor.ainvoke({"query": sql})
        got, gold = parse(out), gold_fn()
        ok = same(got, gold, tol)
        fails += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {qid} {question}")
        print("       工具输出:", " ; ".join(" | ".join(r) for r in got[:4]))
        if not ok:
            print("       标准答案:", gold[:4])

    before = raw.execute("select credit_days from counterparties where name='客户B'").fetchone()[0]
    out = await se.sql_executor.ainvoke({"query": "UPDATE counterparties SET credit_days=90 WHERE name='客户B'"})
    after = sqlite3.connect(DB).execute("select credit_days from counterparties where name='客户B'").fetchone()[0]
    ok = out.startswith("SQL Error: only SELECT") and before == after == 60
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] W1 写请求被拒绝且数据未变（账期 {before} → {after}）")

    out = await se.sql_executor.ainvoke({"query": "SELECT * FROM market_prices WHERE price_month >= '2026-08'"})
    ok = "returned no rows" in out
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] B1 超出数据范围（2026-08 之后行情）返回空，不编造")

    print(f"\n{len(CASES) + 2 - fails}/{len(CASES) + 2} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
