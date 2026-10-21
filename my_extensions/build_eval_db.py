"""生成较大的评测数据库 eval_large.db（确定性：固定随机种子，同一份代码生成同一份数据）。

为什么要有它：demo.db 只有 4 家公司 5 轮融资，用例再多也只是在同一小撮数据上换问法，规模不是真规模。
这个库同样的两张表（companies / funding_rounds，列名一致），30 家公司、6 个行业、76 轮融资，
其中有公司没有融资记录、有 Series D、有金额与日期的分布，能出需要分组、排名、区间过滤的问题。
数据是随机生成的合成数据，公司名为虚构，不对应任何真实公司。
用法: python build_eval_db.py          （已存在则拒绝覆盖）
"""
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "eval_large.db")
SEED = 20261013

INDUSTRIES = ["Humanoid Robotics", "Fintech", "Biotech", "Climate Tech", "Cybersecurity", "Edtech"]
NAMES = ["Quantara", "Lumivex", "Brightforge", "Helixon", "Cobaltree", "Nimbria", "Vantora", "Solenne", "Kestrel Labs",
         "Marlowe AI", "Zephyra", "Ironvale", "Pixelmoor", "Aurelio", "Tessera Bio", "Northwind Data", "Orbitly",
         "Fernlight", "Gridwell", "Halcyon Pay", "Ibexa", "Junoflow", "Krystal Edu", "Lattice Grid", "Mosaiq",
         "Nectar Climate", "Opalworks", "Paragon Sec", "Quillon", "Riverstone Robotics"]
ROUND_ORDER = ["Seed", "Series A", "Series B", "Series C", "Series D"]


def build(path=OUT):
    if os.path.exists(path):
        raise SystemExit(f"拒绝覆盖已存在的 {path}")
    rng = random.Random(SEED)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT, industry TEXT, founded_year INTEGER);
    CREATE TABLE funding_rounds (id INTEGER PRIMARY KEY, company_id INTEGER, round_type TEXT, amount_usd REAL,
        round_date TEXT, FOREIGN KEY(company_id) REFERENCES companies(id));
    """)
    used_years = []
    rid = 0
    for i, name in enumerate(NAMES, start=1):
        industry = INDUSTRIES[(i - 1) % len(INDUSTRIES)]
        year = rng.randint(2012, 2024)
        con.execute("INSERT INTO companies VALUES (?,?,?,?)", (i, name, industry, year))
        used_years.append(year)
        # 每家公司 0~5 轮；约 1/6 的公司没有融资记录
        n = 0 if i % 6 == 0 else rng.randint(1, 5)
        y = max(year, 2016)
        for k in range(n):
            y = min(2026, y + rng.randint(0, 2))
            month = rng.randint(1, 9 if y == 2026 else 12)
            day = rng.randint(1, 28)
            rid += 1
            # 金额 = 随机倍数 + 唯一偏移，避免“最大/最小”并列
            amount = float(rng.randrange(4, 400) * 250000 + rid * 1000)
            con.execute("INSERT INTO funding_rounds VALUES (?,?,?,?,?)",
                        (rid, i, ROUND_ORDER[k], amount, f"{y:04d}-{month:02d}-{day:02d}"))
    con.commit()
    con.close()
    print("ok", path)


if __name__ == "__main__":
    build()
