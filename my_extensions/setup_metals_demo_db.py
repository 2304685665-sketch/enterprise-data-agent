"""生成金属贸易示例台账 metals_demo.db。

数据说明（务必区分真实与模拟）：
- market_prices：真实公开数据。IMF Primary Commodity Prices（经 FRED 转载）的全球铜/铝/锌月均价（美元/吨），
  以及美联储 G.5 的人民币兑美元月均汇率（FRED: EXCHUS）。取数日期 2026-10-20，IMF 数据最新到 2026-07。
  注意：这是国际基准价，不含增值税与区域升贴水，不等于国内现货报价。
- counterparties / trades / receivables / inventory：模拟数据。交易价格围绕上述真实月均价上下浮动生成
  （固定随机种子，可复现）；数量、客户、账期、回款情况均为虚构，只用来演示 Agent 如何查询与分析。
- 公开参照：五矿发展 2025 年年报摘要披露营业收入 528.23 亿元、归母净利润 0.19 亿元（净利率约 0.04%），
  说明大宗金属贸易利润很薄。本示例按公开同业（浙商中拓 2025Q1 毛利率 0.97%）等比例缩小，采购折价 0.1%~0.5%、销售加价 0.4%~1.0%，毛利率约 1.0%，是示意假设，
  不是对任何公司真实毛利的估计。

用法（项目根目录）：
    uv run python my_extensions/setup_metals_demo_db.py
    ODR_SQL_DB_PATH=my_extensions/metals_demo.db <你平时的启动命令>
只会（重新）生成 my_extensions/metals_demo.db，不会改动 demo.db 或任何源码。
"""
import random
import shutil
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "metals_demo.db"
AS_OF = "2026-10-20"

MONTHS = ["2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
          "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
# IMF（FRED: PCOPPUSDM / PALUMUSDM / PZINCUSDM），美元/吨，月均
USD = {
    "copper":    [9770.58, 9671.88, 9994.77, 10739.92, 10812.03, 11790.96, 12986.61, 12951.35, 12528.71, 12890.69, 13512.16, 13552.04, 13542.82],
    "aluminium": [2606.37, 2598.74, 2654.48, 2793.09, 2819.06, 2875.90, 3133.98, 3065.19, 3372.95, 3594.38, 3658.22, 3438.85, 3158.27],
    "zinc":      [2762.56, 2790.38, 2936.74, 3152.00, 3176.84, 3162.18, 3206.73, 3323.71, 3182.26, 3354.62, 3477.64, 3538.74, 3593.78],
}
# 美联储 G.5（FRED: EXCHUS），人民币/美元，月均
FX = [7.1741, 7.1727, 7.1235, 7.1200, 7.1069, 7.0432, 6.9692, 6.9064, 6.8921, 6.8371, 6.7996, 6.7758, 6.7757]
# 2023 全年行情：只写入 market_prices 供历史价格查询，不生成模拟交易（交易仍从 2025-07 起，库存/应收不受影响）。
# 同样取自 FRED（PCOPPUSDM / PALUMUSDM / PZINCUSDM / EXCHUS），取数 2026-10-20，保留 2 位小数。
MONTHS_2023 = [f"2023-{m:02d}" for m in range(1, 13)]
USD_2023 = {
    "copper":    [9007.35, 8936.59, 8856.31, 8809.16, 8243.16, 8396.52, 8476.68, 8347.83, 8276.71, 7941.36, 8189.59, 8407.90],
    "aluminium": [2494.90, 2416.18, 2296.42, 2338.13, 2274.01, 2184.75, 2159.73, 2135.58, 2184.67, 2194.39, 2202.26, 2192.01],
    "zinc":      [3295.88, 3133.84, 2967.46, 2770.36, 2485.78, 2375.45, 2404.65, 2405.16, 2495.50, 2447.18, 2543.61, 2510.57],
}
FX_2023 = [6.7904, 6.8380, 6.8909, 6.8876, 6.9854, 7.1614, 7.1863, 7.2486, 7.2979, 7.3071, 7.2226, 7.1402]
SOURCE = "IMF via FRED (PCOPPUSDM/PALUMUSDM/PZINCUSDM); Fed G.5 via FRED (EXCHUS); 取数 2026-10-20"

CUSTOMERS = {1: ("客户A", 30), 2: ("客户B", 60), 3: ("客户C", 30), 4: ("客户D", 45)}
SUPPLIERS = {5: ("供应商X", 15), 6: ("供应商Y", 30)}
BUY_QTY = {"copper": 20, "aluminium": 60, "zinc": 40}      # 吨/月（虚构）
SHARES = {                                                  # 各客户在销量中的占比（虚构）
    "copper":    {1: .45, 2: .30, 3: .25},
    "aluminium": {1: .35, 2: .30, 3: .25, 4: .10},
    "zinc":      {2: .35, 3: .35, 1: .30},
}
# 价差（相对当月基准价）。按公开同业等比例缩小：浙商中拓 2025Q1 毛利率 0.97%，大型上市贸易商约 1%。
# 买入价低于基准 0.1%~0.5%（均值 0.3%），卖出价高于基准 0.4%~1.0%（均值 0.7%），合计毛利率约 1.0%。
BUY_DISCOUNT = (0.001, 0.005)
SELL_PREMIUM = (0.004, 0.010)
SCALE_ANCHOR = "五矿发展 2025 年营收 528.23 亿元；本库 12 个月销售额约 3,975 万元，缩小比例约 0.075%（仅对金额类、比率类参数有效，人员和固定成本不按此缩小）"
D_ACTIVE_FROM = "2026-05"  # 客户D 自该月起才有交易


def paid_ratio(customer_id: int, month: str) -> float:
    """模拟回款：客户A 只有最近一个月拖欠；B 逐步变慢；C 偶有短付；D 新客户全部未付。"""
    if customer_id == 1:
        return 0.0 if month >= "2026-07" else 1.0
    if customer_id == 2:
        return 1.0 if month <= "2026-02" else (0.5 if month <= "2026-05" else 0.0)
    if customer_id == 3:
        return 0.8 if month == "2026-05" else 1.0
    return 0.0


def build(conn: sqlite3.Connection) -> None:
    rng = random.Random(20261020)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE counterparties (id INTEGER PRIMARY KEY, name TEXT, role TEXT, credit_days INTEGER);
    CREATE TABLE trades (id INTEGER PRIMARY KEY, trade_date TEXT, metal TEXT, direction TEXT,
                         counterparty_id INTEGER, qty_ton REAL, unit_price_cny REAL);
    CREATE TABLE receivables (id INTEGER PRIMARY KEY, trade_id INTEGER, customer_id INTEGER,
                              amount_due_cny REAL, amount_paid_cny REAL, due_date TEXT);
    CREATE TABLE inventory (metal TEXT PRIMARY KEY, qty_ton REAL, avg_cost_cny REAL);
    CREATE TABLE market_prices (price_month TEXT, metal TEXT, usd_per_t REAL, usd_cny REAL,
                                cny_per_t REAL, source TEXT, PRIMARY KEY (price_month, metal));
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)

    cny = {}
    for metal, series in USD.items():
        for m, usd, fx in zip(MONTHS, series, FX):
            c = round(usd * fx, 2)
            cny[(metal, m)] = c
            cur.execute("INSERT INTO market_prices VALUES (?,?,?,?,?,?)", (m, metal, usd, fx, c, SOURCE))

    for metal, series in USD_2023.items():
        for m, usd, fx in zip(MONTHS_2023, series, FX_2023):
            cur.execute("INSERT INTO market_prices VALUES (?,?,?,?,?,?)",
                        (m, metal, usd, fx, round(usd * fx, 2), SOURCE))

    cur.executemany("INSERT INTO counterparties VALUES (?,?,?,?)",
                    [(i, n, "customer", d) for i, (n, d) in CUSTOMERS.items()]
                    + [(i, n, "supplier", d) for i, (n, d) in SUPPLIERS.items()])

    def half(x):  # 取整到 0.5 吨
        return round(x * 2) / 2

    trades = []  # (date, metal, direction, cp, qty, price)
    for m in MONTHS[1:]:
        y, mo = int(m[:4]), int(m[5:])
        for metal in ("copper", "aluminium", "zinc"):
            base = cny[(metal, m)]
            buy_qty = half(BUY_QTY[metal] * rng.uniform(0.9, 1.1))
            supplier = 5 if metal == "copper" else 6 if metal == "aluminium" else (5 if mo % 2 else 6)
            buy_price = round(base * (1 - rng.uniform(*BUY_DISCOUNT)))
            trades.append((date(y, mo, rng.randint(3, 8)).isoformat(), metal, "buy", supplier, buy_qty, buy_price))
            total_sell = buy_qty * 0.85
            shares = {c: s for c, s in SHARES[metal].items() if c != 4 or m >= D_ACTIVE_FROM}
            norm = sum(shares.values())
            for cid, s in shares.items():
                q = half(total_sell * s / norm)
                if q <= 0:
                    continue
                sell_price = round(base * (1 + rng.uniform(*SELL_PREMIUM)))
                trades.append((date(y, mo, rng.randint(10, 25)).isoformat(), metal, "sell", cid, q, sell_price))

    trades.sort(key=lambda t: (t[0], t[1], t[2]))
    for i, t in enumerate(trades, 1):
        cur.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?)", (i, *t))
        d, metal, direction, cp, qty, price = t
        if direction == "sell":
            due = (date.fromisoformat(d) + timedelta(days=CUSTOMERS[cp][1])).isoformat()
            amount = round(qty * price, 2)
            cur.execute("INSERT INTO receivables (trade_id, customer_id, amount_due_cny, amount_paid_cny, due_date) "
                        "VALUES (?,?,?,?,?)", (i, cp, amount, round(amount * paid_ratio(cp, d[:7]), 2), due))

    for metal in ("copper", "aluminium", "zinc"):
        buys = [t for t in trades if t[1] == metal and t[2] == "buy"]
        sells = [t for t in trades if t[1] == metal and t[2] == "sell"]
        qty_in = sum(t[4] for t in buys)
        avg_cost = sum(t[4] * t[5] for t in buys) / qty_in
        cur.execute("INSERT INTO inventory VALUES (?,?,?)",
                    (metal, qty_in - sum(t[4] for t in sells), round(avg_cost, 2)))

    cur.executemany("INSERT INTO meta VALUES (?,?)", [
        ("as_of", AS_OF),
        ("data_note", "market_prices 为真实公开数据（IMF/FRED、美联储 G.5，国际基准价，不含增值税与升贴水）；其余表为模拟数据"),
        ("price_data_last_month", MONTHS[-1]),
        ("scale_note", SCALE_ANCHOR),
        ("price_data_first_month", MONTHS_2023[0]),
        ("inventory_note", "库存 = 期间累计采购 - 累计销售，成本为期间采购加权平均价，不含期初库存"),
    ])


def main() -> None:
    # 先在临时目录建库再复制：某些挂载/同步目录不支持 SQLite 的日志文件与锁
    tmp_dir = tempfile.mkdtemp()
    tmp_db = Path(tmp_dir) / "metals_demo.db"
    conn = sqlite3.connect(tmp_db)
    build(conn)
    conn.commit()
    conn.close()
    shutil.copyfile(tmp_db, DB_PATH)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"metals_demo.db created at {DB_PATH}")


if __name__ == "__main__":
    main()
