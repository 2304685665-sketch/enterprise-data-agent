"""ledger_monthly_report：数字用独立的纯 Python 从库里重算后逐项断言（不经过本模块的 SQL）；小库与放大库各测一遍。
用法：PYTHONPATH=src python my_extensions/test_ledger_monthly.py   需要 metals_demo.db / metals_large.db（先跑对应 setup 脚本）。"""
import asyncio, os, shutil, sqlite3, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
from open_deep_research import sql_executor as se
from open_deep_research import ledger_monthly as lm


def f(x, nd=2): return f"{x:,.{nd}f}"


def check(db_name, month, empty_month):
    src = HERE / db_name
    if not src.exists():
        print(f"⚠️ 跳过 {db_name}：文件不存在"); return
    tmp = Path(tempfile.mkdtemp()) / "m.db"; shutil.copyfile(src, tmp)
    se.DB_PATH = tmp
    c = sqlite3.connect(tmp)
    names = dict(c.execute("select id,name from counterparties"))
    T = c.execute("select id,trade_date,metal,direction,counterparty_id,qty_ton,unit_price_cny from trades").fetchall()
    R = c.execute("select trade_id,customer_id,amount_due_cny,amount_paid_cny,due_date from receivables").fetchall()
    as_of = c.execute("select value from meta where key='as_of'").fetchone()[0]
    text = asyncio.run(lm.ledger_monthly_report.ainvoke({"month": month}))
    M = [t for t in T if t[1][:7] == month]
    sell = [t for t in M if t[3] == "sell"]; buy = [t for t in M if t[3] == "buy"]
    amt = lambda L: sum(t[5] * t[6] for t in L)
    assert f"- sales: {len(sell)} trades, {f(sum(t[5] for t in sell), 1)} t, {f(amt(sell))} CNY" in text, text
    assert f"- purchases: {len(buy)} trades, {f(sum(t[5] for t in buy), 1)} t, {f(amt(buy))} CNY" in text
    for mt in sorted({t[2] for t in T}):
        allbuy = [t for t in T if t[2] == mt and t[3] == "buy"]
        w = sum(t[5] * t[6] for t in allbuy) / sum(t[5] for t in allbuy)
        s = [t for t in sell if t[2] == mt]
        gm = sum(t[5] * (t[6] - w) for t in s)
        assert f"- {mt}: sales {f(sum(t[5] for t in s), 1)} t, {f(amt(s))} CNY, gross margin {f(gm)} CNY (weighted avg buy price {f(w)})" in text, mt
    per = {}
    for t in sell: per[names[t[4]]] = per.get(names[t[4]], 0) + t[5] * t[6]
    top = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    for n, a in top: assert f"- {n}: {f(a)} CNY" in text, n
    ids = {t[0] for t in M if t[3] == "sell"}
    inv = [r for r in R if r[0] in ids]
    ov = [r for r in inv if r[2] > r[3] and r[4] < as_of]
    assert f"- invoices issued: {len(inv)}, billed {f(sum(r[2] for r in inv))} CNY; still unpaid {f(sum(r[2] - r[3] for r in inv))} CNY; of which overdue {len(ov)} invoices, {f(sum(r[2] - r[3] for r in ov))} CNY" in text
    tot = sum(r[2] - r[3] for r in R if r[2] > r[3] and r[4] < as_of)
    assert f"total overdue across all months at {as_of}: {f(tot)} CNY" in text
    et = asyncio.run(lm.ledger_monthly_report.ainvoke({"month": empty_month}))
    assert "no trades in" in et and "Do not extrapolate" in et, et
    print(f"✅ {db_name} {month}：总额/品种毛利/Top5/发票逾期/全期逾期与独立重算一致；空月份如实说明")


def test_all():
    check("metals_demo.db", "2026-06", "2025-07")
    check("metals_large.db", "2026-06", "2025-07")
    check("metals_large.db", "2026-02", "2025-07")


def test_errors():
    se.DB_PATH = HERE / "metals_demo.db"
    for bad in ("2026-13", "26-06", "2026-6", "", "2026-06'; DROP TABLE trades;--"):
        assert asyncio.run(lm.ledger_monthly_report.ainvoke({"month": bad})).startswith("Monthly Report Error"), bad
    assert "later than the ledger as-of" in asyncio.run(lm.ledger_monthly_report.ainvoke({"month": "2026-11"}))
    se.DB_PATH = Path("/nonexistent/x.db")
    assert "not found" in asyncio.run(lm.ledger_monthly_report.ainvoke({"month": "2026-06"}))
    print("✅ 非法月份/注入串/未来月份/缺库均返回明确错误")


if __name__ == "__main__":
    test_all(); test_errors(); print("ALL PASSED")
