"""ledger_briefing：数字用独立的纯 Python 从库里重算后逐项断言（不经过本模块的 SQL）。
用法：PYTHONPATH=src python my_extensions/test_ledger_briefing.py    需要 my_extensions/metals_demo.db（git-ignored，先跑 setup_metals_demo_db.py）。"""
import asyncio, os, shutil, sqlite3, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "metals_demo.db"
if not SRC.exists():
    print("⚠️ 跳过：缺少 metals_demo.db"); sys.exit(0)
tmp = Path(tempfile.mkdtemp()); DB = tmp / "m.db"; shutil.copyfile(SRC, DB)
os.environ["ODR_SQL_DB_PATH"] = str(DB)
from open_deep_research import ledger_briefing as lb

c = sqlite3.connect(DB)
as_of = c.execute("select value from meta where key='as_of'").fetchone()[0]
names = dict(c.execute("select id,name from counterparties"))
R = c.execute("select customer_id,amount_due_cny,amount_paid_cny,due_date from receivables").fetchall()
P = {(m, mt): p for m, mt, p in c.execute("select price_month,metal,cny_per_t from market_prices")}
text = asyncio.run(lb.ledger_briefing.ainvoke({}))

def fmt(x, nd=2): return f"{x:,.{nd}f}"
ov = {}
for cid, due, paid, dd in R:
    if due > paid and dd < as_of: ov[names[cid]] = ov.get(names[cid], 0) + due - paid
total = sum(ov.values())

def test_total_and_customers():
    assert f"TOTAL overdue: {fmt(round(total, 2))} CNY" in text, text
    for n, v in ov.items():
        assert f"- {n}: {fmt(round(v, 2))} CNY" in text, n
    print("✅ 逾期总额与各客户金额与独立重算一致")

def test_prices_and_inventory():
    last = c.execute("select value from meta where key='price_data_last_month'").fetchone()[0]
    cur, prev = P[(last, "copper")], P[("2026-06", "copper")]
    assert f"copper {last}: {fmt(cur)} | month-on-month {(cur / prev - 1) * 100:+.1f}%" in text
    for mt, q, a in c.execute("select metal,qty_ton,avg_cost_cny from inventory"):
        assert f"difference {fmt(P[(last, mt)] - a)}" in text and f"book value {fmt(q * a)}" in text, mt
    print("✅ 行情环比与库存差价/账面价值与独立重算一致")

def test_window_and_errors():
    assert "trades in window: 0" in text and "last trade in the ledger is" in text   # 样例库最后交易早于 as_of 前 7 天
    assert lb.build_briefing(tmp / "nope.db").startswith("Briefing Error: database file not found")
    bad = tmp / "bad.db"; sqlite3.connect(bad).execute("create table x(a)").connection.commit()
    assert "lacks tables" in lb.build_briefing(bad)
    print("✅ 窗口内无交易如实报告；缺库/缺表返回明确错误")

for f in (test_total_and_customers, test_prices_and_inventory, test_window_and_errors): f()
shutil.rmtree(tmp, ignore_errors=True)
