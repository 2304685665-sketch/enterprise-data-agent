"""生成放大版金属贸易台账 metals_large.db（表结构、口径、行情与 metals_demo.db 完全一致，只放大规模）。

数据说明（务必区分真实与模拟）：
- market_prices：真实公开数据，直接复用 setup_metals_demo_db 里的 IMF/FRED 月均价与美联储 G.5 汇率。
- counterparties / trades / receivables / inventory：模拟数据，固定随机种子可复现；40 个客户、12 个供应商，
  客户分四类回款行为（准时 / 偏慢 / 拖欠 / 新客户），交易笔数约为小库的二十倍。
- 目的：检验在更大数据量、更多实体下，指标字典、空结果提示、确定性周报等机制是否仍然成立。
  它不是任何真实公司的数据，也不代表真实业务量。

用法（项目根目录）：
    python3 my_extensions/setup_metals_large_db.py
只会（重新）生成 my_extensions/metals_large.db。
"""
import random
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_metals_demo_db as small  # 复用真实行情常量与 AS_OF

DB_PATH = Path(__file__).resolve().parent / "metals_large.db"
SEED = 20261021
N_CUSTOMERS, N_SUPPLIERS = 40, 12
MONTHS = small.MONTHS[1:]           # 交易区间与小库一致：2025-08 ~ 2026-07
METALS = ("copper", "aluminium", "zinc")


def profile_for(cid: int, rng: random.Random) -> str:
    r = rng.random()
    return "good" if r < 0.55 else "slow" if r < 0.80 else "bad" if r < 0.92 else "new"


def paid_ratio(profile: str, month: str, rng: random.Random, active_from: str) -> float:
    if profile == "good":
        return 1.0 if month <= "2026-05" else rng.choice([1.0, 1.0, 0.0])
    if profile == "slow":
        return 1.0 if month <= "2026-03" else rng.choice([1.0, 0.7, 0.5, 0.0])
    if profile == "bad":
        return 1.0 if month <= "2025-12" else rng.choice([0.0, 0.0, 0.3, 0.5])
    return rng.choice([0.0, 1.0]) if month >= active_from else 1.0   # new


def build(conn: sqlite3.Connection) -> None:
    rng = random.Random(SEED)
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
    for metal, series in small.USD.items():
        for m, usd, fx in zip(small.MONTHS, series, small.FX):
            c = round(usd * fx, 2)
            cny[(metal, m)] = c
            cur.execute("INSERT INTO market_prices VALUES (?,?,?,?,?,?)", (m, metal, usd, fx, c, small.SOURCE))
    for metal, series in small.USD_2023.items():
        for m, usd, fx in zip(small.MONTHS_2023, series, small.FX_2023):
            cur.execute("INSERT INTO market_prices VALUES (?,?,?,?,?,?)",
                        (m, metal, usd, fx, round(usd * fx, 2), small.SOURCE))

    customers, suppliers = {}, {}
    for i in range(1, N_CUSTOMERS + 1):
        customers[i] = (f"客户{i:02d}", rng.choice([30, 30, 45, 60, 60, 90]))
    for j in range(N_SUPPLIERS):
        suppliers[N_CUSTOMERS + 1 + j] = (f"供应商{j + 1:02d}", rng.choice([15, 30]))
    cur.executemany("INSERT INTO counterparties VALUES (?,?,?,?)",
                    [(i, n, "customer", d) for i, (n, d) in customers.items()]
                    + [(i, n, "supplier", d) for i, (n, d) in suppliers.items()])

    prof, buys_metals, active_from = {}, {}, {}
    for cid in customers:
        prof[cid] = profile_for(cid, rng)
        buys_metals[cid] = rng.sample(METALS, rng.choice([1, 2, 2, 3]))
        active_from[cid] = rng.choice(MONTHS[7:]) if prof[cid] == "new" else MONTHS[0]

    half = lambda x: round(x * 2) / 2
    sells, buys = [], []
    for m in MONTHS:
        y, mo = int(m[:4]), int(m[5:])
        for metal in METALS:
            base = cny[(metal, m)]
            month_sell = 0.0
            for cid in customers:
                if metal not in buys_metals[cid] or m < active_from[cid]:
                    continue
                for _ in range(rng.choice([0, 1, 2, 3, 3, 4, 5])):
                    q = half(rng.uniform(2, 14))
                    if q <= 0:
                        continue
                    price = round(base * (1 + rng.uniform(0.008, 0.020)))
                    sells.append((date(y, mo, rng.randint(10, 25)).isoformat(), metal, "sell", cid, q, price))
                    month_sell += q
            need = month_sell / 0.85
            k = rng.randint(3, 5)
            for s in range(k):
                q = half(need / k) or 0.5
                buys.append((date(y, mo, rng.randint(2, 9)).isoformat(), metal, "buy",
                             rng.choice(list(suppliers)), q, round(base * (1 - rng.uniform(0.002, 0.008)))))
    trades = sorted(sells + buys, key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
    for i, t in enumerate(trades, 1):
        cur.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?)", (i, *t))
        d, metal, direction, cp, qty, price = t
        if direction == "sell":
            due = (date.fromisoformat(d) + timedelta(days=customers[cp][1])).isoformat()
            amount = round(qty * price, 2)
            ratio = paid_ratio(prof[cp], d[:7], rng, active_from[cp])
            cur.execute("INSERT INTO receivables (trade_id, customer_id, amount_due_cny, amount_paid_cny, due_date) "
                        "VALUES (?,?,?,?,?)", (i, cp, amount, round(amount * ratio, 2), due))
    for metal in METALS:
        b = [t for t in trades if t[1] == metal and t[2] == "buy"]
        s = [t for t in trades if t[1] == metal and t[2] == "sell"]
        qty_in = sum(t[4] for t in b)
        cur.execute("INSERT INTO inventory VALUES (?,?,?)",
                    (metal, qty_in - sum(t[4] for t in s), round(sum(t[4] * t[5] for t in b) / qty_in, 2)))
    cur.executemany("INSERT INTO meta VALUES (?,?)", [
        ("as_of", small.AS_OF),
        ("data_note", "market_prices 为真实公开数据（IMF/FRED、美联储 G.5，国际基准价，不含增值税与升贴水）；其余表为模拟数据（放大版）"),
        ("price_data_last_month", small.MONTHS[-1]),
        ("price_data_first_month", small.MONTHS_2023[0]),
        ("inventory_note", "库存 = 期间累计采购 - 累计销售，成本为期间采购加权平均价，不含期初库存"),
    ])


def main() -> None:
    tmp_dir = tempfile.mkdtemp()
    tmp_db = Path(tmp_dir) / "metals_large.db"
    conn = sqlite3.connect(tmp_db)
    build(conn)
    conn.commit()
    for t in ("counterparties", "trades", "receivables", "inventory", "market_prices"):
        print(t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    conn.close()
    shutil.copyfile(tmp_db, DB_PATH)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"metals_large.db created at {DB_PATH}")


if __name__ == "__main__":
    main()
