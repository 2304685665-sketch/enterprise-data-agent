"""统计类留出集 stats：12 题，在 eval_gen.db（订单库）上，运行前冻结。

回答的问题：给 SQL 工具加数据库内统计函数（ODR_SQL_STATS_FUNCS，见 sql_executor.py）是否只修好了开发时那一道题
（main2 的 L-stats-pstdev）？这 12 题都是新写的、换了数据库，覆盖总体/样本标准差、中位数，以及 状态/地区/品类/年份/数量 过滤。
期望值全部由 Python statistics 在数据库行上现算；每个子集都断言至少 3 行。题目在看到任何 S- 运行结果之前写定。
"""
from __future__ import annotations

import os
import statistics
from typing import Any

from agent_eval_cases import _connect, _q

STATS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_gen.db")


def build_stats_cases(db_path: str = STATS_DB) -> list[dict[str, Any]]:
    c = _connect(db_path)
    try:
        cases: list[dict[str, Any]] = []

        def vals(sql, *a):
            v = [r[0] for r in c.execute(sql, a).fetchall() if r[0] is not None]
            assert len(v) >= 3, sql
            return v

        def add(cid, q, value):
            cases.append({"id": cid, "category": "python_stats", "question": _q(q), "expected": {"kind": "money", "value": float(value)}})

        allv = vals("SELECT amount_usd FROM orders")
        paid = vals("SELECT amount_usd FROM orders WHERE status='paid'")
        east = vals("SELECT o.amount_usd FROM orders o JOIN customers c ON c.customer_id=o.customer_id WHERE c.region='East'")
        central = vals("SELECT o.amount_usd FROM orders o JOIN customers c ON c.customer_id=o.customer_id WHERE c.region='Central'")
        home = vals("SELECT o.amount_usd FROM orders o JOIN products p ON p.product_id=o.product_id WHERE p.category='Home'")
        kitchen = vals("SELECT o.amount_usd FROM orders o JOIN products p ON p.product_id=o.product_id WHERE p.category='Kitchen'")
        y2025 = vals("SELECT amount_usd FROM orders WHERE order_date LIKE '2025-%'")
        q3 = vals("SELECT amount_usd FROM orders WHERE quantity >= 3")
        prices = vals("SELECT unit_price FROM products")

        add("S-pstdev-all", "所有订单金额的总体标准差是多少美元？", statistics.pstdev(allv))
        add("S-stdev-all", "所有订单金额的样本标准差（n-1）是多少美元？", statistics.stdev(allv))
        add("S-median-all", "所有订单金额的中位数是多少美元？", statistics.median(allv))
        add("S-pstdev-paid", "状态为 paid 的订单，金额的总体标准差是多少美元？", statistics.pstdev(paid))
        add("S-median-paid", "状态为 paid 的订单，金额的中位数是多少美元？", statistics.median(paid))
        add("S-stdev-east", "East 地区客户下的订单，金额的样本标准差（n-1）是多少美元？", statistics.stdev(east))
        add("S-median-central", "Central 地区客户下的订单，金额的中位数是多少美元？", statistics.median(central))
        add("S-pstdev-home", "Home 品类商品的订单，金额的总体标准差是多少美元？", statistics.pstdev(home))
        add("S-median-kitchen", "Kitchen 品类商品的订单，金额的中位数是多少美元？", statistics.median(kitchen))
        add("S-pstdev-2025", "2025 年的订单，金额的总体标准差是多少美元？", statistics.pstdev(y2025))
        add("S-median-qty3", "数量大于等于 3 的订单，金额的中位数是多少美元？", statistics.median(q3))
        add("S-stdev-price", "所有商品的单价（unit_price）的样本标准差（n-1）是多少美元？", statistics.stdev(prices))
        return cases
    finally:
        c.close()
