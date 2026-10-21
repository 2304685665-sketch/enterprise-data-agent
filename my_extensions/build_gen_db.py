"""生成“换领域”泛化评测库 eval_gen.db（确定性：固定种子）。

目的：schema 提示（ODR_SQL_SCHEMA_HINT）与直连模式此前只在 companies / funding_rounds 的库上测过。
这里换成完全不同的领域和表结构：customers / products / orders，表名、列名、取值都不同，
用来检验这些改进是否只是“适配了那一个库”。数据为随机合成，名称虚构。
用法: python build_gen_db.py [输出路径]   （已存在则拒绝覆盖）
"""
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "eval_gen.db")
SEED = 20261014
REGIONS = ["East", "West", "North", "South", "Central"]
CUSTOMERS = ["Alderwick Foods", "Bluepeak Retail", "Cinder & Co", "Dovetail Home", "Eastgate Mart", "Fjord Outfitters",
             "Glenhaven Pharmacy", "Harborline", "Ironbridge Tools", "Juniper Market", "Kitebrook", "Larkspur Gifts",
             "Meridian Supply", "Northmoor Books", "Oakhurst Garden", "Pinecrest Pets", "Quarry Lane", "Redfern Toys",
             "Silverleaf", "Tidewater Goods", "Umberfield", "Violet Row", "Westbrook Hardware", "Yarrow Foods", "Zinnia Home"]
PRODUCTS = [("Aero Kettle", "Kitchen", 42.0), ("Brio Blender", "Kitchen", 89.0), ("Cove Lamp", "Home", 35.5),
            ("Delta Drill", "Tools", 129.0), ("Ember Heater", "Home", 64.0), ("Flux Router", "Tools", 98.5),
            ("Gale Fan", "Home", 27.0), ("Haze Humidifier", "Home", 54.0), ("Isle Toaster", "Kitchen", 31.0),
            ("Jolt Charger", "Electronics", 19.5), ("Kilo Scale", "Kitchen", 23.0), ("Lumen Torch", "Tools", 16.5)]
STATUSES = ["paid", "paid", "paid", "pending", "refunded"]


def build(path=OUT):
    if os.path.exists(path):
        raise SystemExit(f"拒绝覆盖已存在的 {path}")
    rng = random.Random(SEED)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, customer_name TEXT, region TEXT, signup_year INTEGER);
    CREATE TABLE products (product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT, unit_price REAL);
    CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, product_id INTEGER, quantity INTEGER,
        amount_usd REAL, order_date TEXT, status TEXT,
        FOREIGN KEY(customer_id) REFERENCES customers(customer_id), FOREIGN KEY(product_id) REFERENCES products(product_id));
    """)
    for i, n in enumerate(CUSTOMERS, 1):
        con.execute("INSERT INTO customers VALUES (?,?,?,?)", (i, n, REGIONS[(i - 1) % 5], rng.randint(2018, 2025)))
    for i, (n, cat, p) in enumerate(PRODUCTS, 1):
        con.execute("INSERT INTO products VALUES (?,?,?,?)", (i, n, cat, p))
    oid = 0
    for i in range(1, len(CUSTOMERS) + 1):
        n = 0 if i % 8 == 0 else rng.randint(1, 7)           # 部分客户没有订单
        for _ in range(n):
            oid += 1
            pid = rng.randint(1, len(PRODUCTS)); qty = rng.randint(1, 20)
            price = PRODUCTS[pid - 1][2]
            amount = round(price * qty + oid * 0.01, 2)       # 加唯一偏移，避免最大/最小并列
            y = rng.choice([2024, 2025, 2026]); m = rng.randint(1, 9 if y == 2026 else 12); d = rng.randint(1, 28)
            con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
                        (oid, i, pid, qty, amount, f"{y:04d}-{m:02d}-{d:02d}", rng.choice(STATUSES)))
    con.commit(); con.close()
    print("ok", path)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else OUT)
