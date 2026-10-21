"""ledger_monthly_report 工具 —— 金属台账月报的确定性数据块（ODR_LEDGER_MONTHLY=1 才注册，默认关闭）。

与 ledger_briefing 同一思路：月报里的所有数字由固定的参数化 SQL 在只读连接上一次算好，模型只负责引用与措辞，不自己加总。
参数只有一个 month（YYYY-MM），先用正则校验再作为绑定参数传入，没有 SQL 注入面。

口径（与 metals_metric_dictionary.md 一致）：
- 销售额 / 采购额只按 direction 分别统计，不相加。
- 毛利（按品种）= 该月销售量 ×（销售价 − 该品种全期加权平均采购价），全期加权平均采购价 = Σ(采购量×采购价)/Σ采购量。
- 逾期 = amount_due > amount_paid 且 due_date < as_of；本报告只统计"该月开出的销售发票"在 as_of 时的逾期情况。
- 库存为 as_of 时点的账面库存（不是月末库存）。

边界：只服务这套表结构；month 晚于 as_of 所在月时返回错误；月内没有交易时如实报告并给出最后一笔交易日，不外推。
"""
import asyncio
import re
import sqlite3
from pathlib import Path

from langchain_core.tools import tool

from open_deep_research import sql_executor as _se

MONTHLY_DESCRIPTION = (
    "Return the complete, pre-computed monthly report figures for the internal metals ledger for one calendar month "
    "(argument month as YYYY-MM): sales and purchase totals and trade counts, per-metal sales volume / sales amount / gross margin, "
    "top customers by sales, benchmark price and month-on-month change, overdue status (as of the ledger's as-of date) of the invoices "
    "issued that month, and current inventory. Use it for any monthly report / monthly review request and quote its numbers verbatim -- "
    "do not recompute, round or re-add them."
)


def _f(x, nd=2):
    return f"{x:,.{nd}f}"


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) - 1
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def build_monthly(db_path, month: str) -> str:
    month = (month or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return "Monthly Report Error: month must look like YYYY-MM (for example 2026-06)."
    path = Path(db_path)
    if not path.exists():
        return f"Monthly Report Error: database file not found at {path}."
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"trades", "receivables", "inventory", "market_prices", "counterparties", "meta"}
        if not need <= have:
            return f"Monthly Report Error: this database lacks tables {sorted(need - have)}; the tool only supports the metals ledger schema."
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        as_of = meta.get("as_of")
        if not as_of:
            return "Monthly Report Error: meta.as_of is missing."
        if month > as_of[:7]:
            return f"Monthly Report Error: month {month} is later than the ledger as-of date {as_of}."
        last_trade = conn.execute("SELECT MAX(trade_date) FROM trades").fetchone()[0]
        out = [f"# Monthly ledger report data: {month} (ledger as of {as_of}; last trade date {last_trade})", ""]
        n = conn.execute("SELECT COUNT(*) FROM trades WHERE substr(trade_date,1,7)=?", (month,)).fetchone()[0]
        if n == 0:
            out.append(f"- no trades in {month}; the last trade in the ledger is {last_trade}. Do not extrapolate.")
        else:
            out.append("## 1. Totals (sales and purchases are reported separately and must never be added)")
            for d, label in (("sell", "sales"), ("buy", "purchases")):
                cnt, qty, amt = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(qty_ton),0), COALESCE(SUM(qty_ton*unit_price_cny),0) FROM trades "
                    "WHERE substr(trade_date,1,7)=? AND direction=?", (month, d)).fetchone()
                out.append(f"- {label}: {cnt} trades, {_f(qty, 1)} t, {_f(amt)} CNY ({amt / 10000:,.2f} 万元)")
            out += ["", "## 2. Per metal (gross margin = sales qty x (sell price - full-period weighted average buy price))"]
            for mt in [r[0] for r in conn.execute("SELECT DISTINCT metal FROM trades ORDER BY metal")]:
                w = conn.execute("SELECT SUM(qty_ton*unit_price_cny)/SUM(qty_ton) FROM trades WHERE metal=? AND direction='buy'", (mt,)).fetchone()[0]
                q, a, gm = conn.execute(
                    "SELECT COALESCE(SUM(qty_ton),0), COALESCE(SUM(qty_ton*unit_price_cny),0), COALESCE(SUM(qty_ton*(unit_price_cny-?)),0) "
                    "FROM trades WHERE metal=? AND direction='sell' AND substr(trade_date,1,7)=?", (w, mt, month)).fetchone()
                out.append(f"- {mt}: sales {_f(q, 1)} t, {_f(a)} CNY, gross margin {_f(gm)} CNY (weighted avg buy price {_f(w)})")
            out += ["", "## 3. Top 5 customers by sales"]
            for nme, a, q in conn.execute(
                    "SELECT cp.name, SUM(t.qty_ton*t.unit_price_cny) a, SUM(t.qty_ton) FROM trades t JOIN counterparties cp ON cp.id=t.counterparty_id "
                    "WHERE t.direction='sell' AND substr(t.trade_date,1,7)=? GROUP BY cp.name ORDER BY a DESC, cp.name LIMIT 5", (month,)):
                out.append(f"- {nme}: {_f(a)} CNY, {_f(q, 1)} t")
        out += ["", "## 4. Benchmark prices (CNY per tonne; international benchmark, not domestic spot, excl. VAT)"]
        pm = _prev_month(month)
        rows = conn.execute("SELECT metal, cny_per_t FROM market_prices WHERE price_month=? ORDER BY metal", (month,)).fetchall()
        if not rows:
            out.append(f"- no benchmark price rows for {month} (latest price month in the ledger: {meta.get('price_data_last_month')}); say there is no data.")
        prev = dict(conn.execute("SELECT metal, cny_per_t FROM market_prices WHERE price_month=?", (pm,)).fetchall())
        for mt, p in rows:
            mom = f"{(p / prev[mt] - 1) * 100:+.1f}%" if prev.get(mt) else "n/a"
            out.append(f"- {mt} {month}: {_f(p)} | month-on-month {mom}")
        out += ["", f"## 5. Invoices issued in {month}: status at {as_of}"]
        inv_n, inv_amt, unpaid = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(r.amount_due_cny),0), COALESCE(SUM(r.amount_due_cny-r.amount_paid_cny),0) FROM receivables r "
            "JOIN trades t ON t.id=r.trade_id WHERE substr(t.trade_date,1,7)=?", (month,)).fetchone()
        ov_n, ov_amt = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(r.amount_due_cny-r.amount_paid_cny),0) FROM receivables r JOIN trades t ON t.id=r.trade_id "
            "WHERE substr(t.trade_date,1,7)=? AND r.amount_due_cny>r.amount_paid_cny AND r.due_date<?", (month, as_of)).fetchone()
        out.append(f"- invoices issued: {inv_n}, billed {_f(inv_amt)} CNY; still unpaid {_f(unpaid)} CNY; of which overdue {ov_n} invoices, {_f(ov_amt)} CNY ({ov_amt / 10000:,.2f} 万元)")
        tot = conn.execute("SELECT COALESCE(SUM(amount_due_cny-amount_paid_cny),0) FROM receivables WHERE amount_due_cny>amount_paid_cny AND due_date<?", (as_of,)).fetchone()[0]
        out.append(f"- for reference, total overdue across all months at {as_of}: {_f(tot)} CNY ({tot / 10000:,.2f} 万元)")
        out += ["", f"## 6. Inventory at {as_of} (book stock, not month-end)"]
        for mt, q, a in conn.execute("SELECT metal, qty_ton, avg_cost_cny FROM inventory ORDER BY metal"):
            out.append(f"- {mt}: {_f(q, 1)} t, avg_cost {_f(a)}, book value {_f(q * a)}")
        out += ["", "Definitions: as above; all amounts in CNY; ledger data is simulated, benchmark prices are public IMF/FRED data."]
        return "\n".join(out)
    except sqlite3.Error as e:
        return f"Monthly Report Error: {e}"
    finally:
        conn.close()


@tool(description=MONTHLY_DESCRIPTION)
async def ledger_monthly_report(month: str) -> str:
    """Pre-computed monthly report figures for the internal metals ledger (month = YYYY-MM)."""
    return await asyncio.to_thread(build_monthly, _se.DB_PATH, month)
