"""ledger_briefing 工具 —— 金属台账周报的确定性数据块（ODR_LEDGER_BRIEFING=1 才注册，默认关闭）。

动机（作者在模拟台账上的真实模型运行）：让模型自己拼多条 SQL 写周报时，"逾期总额"等关键数字有时缺失或被心算写错
（简报 1/3 通过）。这里把行情、逾期、库存三块的所有数字用固定 SQL 一次算好，模型只负责引用与措辞，不再自己加总。

边界（诚实）：
- 只服务于 metals_demo.db 这套表结构（trades / receivables / inventory / market_prices / counterparties / meta）；表缺失时返回明确错误，不猜。
- 连接与 sql_executor 一样是 mode=ro + PRAGMA query_only=ON；不接受任何参数，因此没有 SQL 注入面。
- "本周"定义为 as_of 之前 7 天；台账最后一笔交易早于该窗口时，只如实报告"窗口内无交易"和最后交易日，不外推。
- 它保证数字是库里算出来的，不保证模型转述正确。
"""
import asyncio
import sqlite3
from pathlib import Path

from langchain_core.tools import tool

from open_deep_research import sql_executor as _se

LEDGER_BRIEFING_DESCRIPTION = (
    "Return the complete, pre-computed weekly briefing figures for the internal metals ledger as a text block: "
    "as-of date, latest benchmark prices with month-on-month and year-on-year changes, overdue receivables "
    "(total, per customer, aging buckets), inventory versus the latest benchmark, and trade activity in the 7 days up to the "
    "as-of date. Takes no arguments. Use it for any weekly / periodic briefing request and quote its numbers verbatim -- "
    "do not recompute, round or re-add them."
)


def _prev_month(ym: str, back: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) - back
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _f(x, nd=2):
    return f"{x:,.{nd}f}"


def build_briefing(db_path) -> str:
    path = Path(db_path)
    if not path.exists():
        return f"Briefing Error: database file not found at {path}."
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"trades", "receivables", "inventory", "market_prices", "counterparties", "meta"}
        if not need <= have:
            return f"Briefing Error: this database lacks tables {sorted(need - have)}; the briefing tool only supports the metals ledger schema."
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        as_of = meta.get("as_of")
        if not as_of:
            return "Briefing Error: meta.as_of is missing."
        out = [f"# Weekly ledger briefing data (as of {as_of})", ""]
        last_trade = conn.execute("SELECT MAX(trade_date) FROM trades").fetchone()[0]
        out.append(f"- as_of: {as_of}; last trade date in ledger: {last_trade}; latest benchmark price month: {meta.get('price_data_last_month')}")
        for k in ("data_note", "inventory_note"):
            if meta.get(k):
                out.append(f"- {k}: {meta[k]}")

        out += ["", "## 1. Benchmark prices (CNY per tonne; international benchmark, not domestic spot, excl. VAT)"]
        last_m = meta.get("price_data_last_month") or conn.execute("SELECT MAX(price_month) FROM market_prices").fetchone()[0]
        prices = {(m, mt): p for m, mt, p in conn.execute("SELECT price_month, metal, cny_per_t FROM market_prices")}
        metals = sorted({mt for _, mt in prices})
        for mt in metals:
            cur, prev, yago = prices.get((last_m, mt)), prices.get((_prev_month(last_m, 1), mt)), prices.get((_prev_month(last_m, 12), mt))
            mom = f"{(cur / prev - 1) * 100:+.1f}%" if cur and prev else "n/a"
            yoy = f"{(cur / yago - 1) * 100:+.1f}%" if cur and yago else "n/a"
            out.append(f"- {mt} {last_m}: {_f(cur)} | month-on-month {mom} | year-on-year {yoy}")

        out += ["", "## 2. Overdue receivables (unpaid and due_date < as_of; overdue amount = amount_due - amount_paid)"]
        rows = conn.execute(
            "SELECT cp.name, ROUND(SUM(r.amount_due_cny - r.amount_paid_cny), 2), COUNT(*), "
            "CAST(MAX(julianday(?) - julianday(r.due_date)) AS INTEGER) FROM receivables r JOIN counterparties cp ON cp.id = r.customer_id "
            "WHERE r.amount_due_cny > r.amount_paid_cny AND r.due_date < ? GROUP BY cp.name ORDER BY 2 DESC", (as_of, as_of)).fetchall()
        total = round(sum(r[1] for r in rows), 2)
        out.append(f"- TOTAL overdue: {_f(total)} CNY ({total / 10000:,.2f} 万元), {sum(r[2] for r in rows)} invoices, {len(rows)} customers")
        for n, s, c, d in rows:
            out.append(f"- {n}: {_f(s)} CNY, {c} invoices, longest overdue {d} days")
        buckets = conn.execute(
            "SELECT CASE WHEN d <= 30 THEN '1-30' WHEN d <= 60 THEN '31-60' WHEN d <= 90 THEN '61-90' ELSE '>90' END b, ROUND(SUM(a), 2) FROM ("
            "SELECT julianday(?) - julianday(due_date) d, amount_due_cny - amount_paid_cny a FROM receivables "
            "WHERE amount_due_cny > amount_paid_cny AND due_date < ?) GROUP BY b", (as_of, as_of)).fetchall()
        bd = dict(buckets)
        out.append("- aging (days overdue): " + "; ".join(f"{b}: {_f(bd.get(b, 0))}" for b in ("1-30", "31-60", "61-90", ">90")))
        not_due = conn.execute("SELECT ROUND(COALESCE(SUM(amount_due_cny - amount_paid_cny), 0), 2) FROM receivables WHERE amount_due_cny > amount_paid_cny AND due_date >= ?", (as_of,)).fetchone()[0]
        out.append(f"- unpaid but not yet due: {_f(not_due)} CNY")

        out += ["", "## 3. Inventory versus latest benchmark (difference = benchmark - avg_cost; positive = cost below market, unrealised gain; negative = cost above market, write-down risk)"]
        for mt, q, a in conn.execute("SELECT metal, qty_ton, avg_cost_cny FROM inventory ORDER BY metal"):
            b = prices.get((last_m, mt))
            diff = b - a if b is not None else None
            tag = "" if diff is None else (" (cost below market)" if diff > 0 else " (cost ABOVE market)")
            out.append(f"- {mt}: {q} t, avg_cost {_f(a)}, benchmark {_f(b) if b is not None else 'n/a'}, difference {_f(diff) if diff is not None else 'n/a'}{tag}, book value {_f(q * a)}")

        out += ["", "## 4. Trades in the 7 days up to as_of"]
        n = conn.execute("SELECT COUNT(*) FROM trades WHERE trade_date > date(?, '-7 day') AND trade_date <= ?", (as_of, as_of)).fetchone()[0]
        out.append(f"- trades in window: {n}" + ("" if n else f" -- no trades; the last trade in the ledger is {last_trade}"))
        out += ["", "Definitions: overdue / inventory / price rules as above; all amounts in CNY; ledger data is simulated, benchmark prices are public IMF/FRED data."]
        return "\n".join(out)
    except sqlite3.Error as e:
        return f"Briefing Error: {e}"
    finally:
        conn.close()


@tool(description=LEDGER_BRIEFING_DESCRIPTION)
async def ledger_briefing() -> str:
    """Pre-computed weekly briefing figures for the internal metals ledger."""
    return await asyncio.to_thread(build_briefing, _se.DB_PATH)
