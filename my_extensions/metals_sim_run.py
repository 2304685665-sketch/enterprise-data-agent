"""模拟运行：只跑“不需要大模型/外网”的真实工具层，并生成一份简报样例。

真实运行的部分（项目自带代码）：
- sql_executor（只读保护、SELECT 限制）对 metals_demo.db 执行查询
- hybrid_retrieval.BM25Index 对 knowledge_base_metals/ 里的公开资料做检索（只有 BM25，没有向量部分）
没有运行的部分：
- 主管/研究员的模型规划和写作（需要 OpenAI/Tavily，沙箱联不上）。下面的 SQL 是我替模型写的，简报由模板按查询结果拼出，不是模型生成。
- python_executor（需要 Docker）、internal_doc_search 的向量检索（需要 OpenAI 嵌入）。

用法（项目根目录）： uv run python my_extensions/metals_sim_run.py
只依赖 langchain-core；数据库先复制到临时目录再只读打开；只写 my_extensions/metals_sim_run/ 下两个 md 文件。
"""
import asyncio
import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "metals_sim_run"
TMP = Path(tempfile.mkdtemp())
DB = TMP / "metals_demo.db"
shutil.copyfile(HERE / "metals_demo.db", DB)
os.environ["ODR_SQL_DB_PATH"] = str(DB)  # 必须在加载 sql_executor 之前


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, HERE.parent / "src" / "open_deep_research" / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


se = load("sql_executor_mod", "sql_executor.py")
hr = load("hybrid_retrieval_mod", "hybrid_retrieval.py")

log = []


def say(s=""):
    log.append(s)
    print(s)


async def q(sql):
    out = await se.sql_executor.ainvoke({"query": sql})
    return out


def rows(out):
    lines = out.split("\n")
    return [l.split(" | ") for l in lines[1:] if l.strip()]


def chunks():
    items = []
    for f in sorted((HERE / "knowledge_base_metals").glob("*.md")):
        cur, title = [], "开头"
        for line in f.read_text(encoding="utf-8").split("\n"):
            if line.startswith("## "):
                if cur:
                    items.append((f.stem, title, "\n".join(cur).strip()))
                title, cur = line[3:].strip(), [line]
            else:
                cur.append(line)
        if cur:
            items.append((f.stem, title, "\n".join(cur).strip()))
    return items


async def main():
    say("# 模拟运行记录（工具层真实运行，模型层未运行）\n")
    say("## 一、公开资料检索（项目自带 BM25，无向量部分）\n")
    items = chunks()
    bm = hr.BM25Index([t for _, _, t in items])
    for query in ["五矿发展 营业收入 净利润", "应收账款 信用减值 计提", "为什么不做价格预测",
                  "智能体 供应链 风险 知识图谱", "上海期货交易所 今日铜现货报价"]:
        idx = bm.rank(query)[:2]
        say(f"- 查询：{query}")
        if not idx:
            say("  - 无词项重叠，未命中")
        for i in idx:
            d, sec, txt = items[i]
            say(f"  - {d} / {sec}：{txt.splitlines()[-1][:60] if False else txt.replace(chr(10), ' ')[:70]}…")
    say()

    say("## 二、台账与行情查询（sql_executor，真实运行）\n")
    price = rows(await q(
        "SELECT a.metal, ROUND(a.usd_per_t,0), ROUND((a.usd_per_t/b.usd_per_t-1)*100,1), ROUND((a.usd_per_t/c.usd_per_t-1)*100,1) "
        "FROM market_prices a JOIN market_prices b ON b.metal=a.metal AND b.price_month='2026-06' "
        "JOIN market_prices c ON c.metal=a.metal AND c.price_month='2025-07' WHERE a.price_month='2026-07' ORDER BY a.metal"))
    overdue = rows(await q(
        "SELECT cp.name, ROUND(SUM(r.amount_due_cny-r.amount_paid_cny),0) FROM receivables r JOIN counterparties cp ON cp.id=r.customer_id "
        "WHERE r.due_date < (SELECT value FROM meta WHERE key='as_of') AND r.amount_due_cny > r.amount_paid_cny GROUP BY cp.name ORDER BY 2 DESC"))
    old = rows(await q(
        "SELECT DISTINCT cp.name FROM receivables r JOIN counterparties cp ON cp.id=r.customer_id WHERE r.amount_due_cny > r.amount_paid_cny "
        "AND julianday((SELECT value FROM meta WHERE key='as_of')) - julianday(r.due_date) > 90 ORDER BY cp.name"))
    inv = rows(await q(
        "SELECT i.metal, ROUND(i.avg_cost_cny,0), ROUND(m.cny_per_t,0), ROUND(m.cny_per_t - i.avg_cost_cny,0) FROM inventory i "
        "JOIN market_prices m ON m.metal=i.metal AND m.price_month='2026-07' ORDER BY i.metal"))
    for name, r in [("行情环比/同比", price), ("逾期未收", overdue), ("逾期超 90 天客户", old), ("库存成本与基准价", inv)]:
        say(f"- {name}：" + " ; ".join(" | ".join(x) for x in r))
    say()

    say("## 三、边界测试\n")
    for label, sql in [("写入", "INSERT INTO inventory VALUES ('lead', 1, 1)"),
                       ("删除", "DELETE FROM trades"),
                       ("挂载外部库", "ATTACH DATABASE 'x.db' AS e"),
                       ("PRAGMA 写入", "PRAGMA user_version = 7"),
                       ("超出数据范围", "SELECT * FROM market_prices WHERE price_month >= '2026-08'")]:
        out = (await q(sql)).split("\n")[0][:90]
        say(f"- {label}：{out}")

    # ---- 简报样例（模板拼接，数字全部来自上面的查询结果）----
    cn = {"aluminium": "铝", "copper": "铜", "zinc": "锌"}
    b = ["# 金属行情与台账简报（样例，非模型生成）\n",
         "> 行情为 IMF 月度基准价（经 FRED），美元/吨，不是上期所/LME 的日度现货价，不能用于报价；台账为虚构示例数据。\n",
         "## 一、行情（2026-07，较上月 / 较 2025-07）\n",
         "| 品种 | 美元/吨 | 环比 | 同比 |", "| --- | --- | --- | --- |"]
    for m, usd, mom, yoy in price:
        b.append(f"| {cn[m]} | {float(usd):,.0f} | {float(mom):+.1f}% | {float(yoy):+.1f}% |")
    b += ["", "## 二、台账（虚构）\n",
          "- 逾期未收：" + "；".join(f"{n} {float(v) / 1e4:,.1f} 万元" for n, v in overdue) + "。",
          "- 逾期超过 90 天的客户：" + "、".join(n for (n,) in old) + "，建议优先核对合同和对账单后再跟进。",
          "- 库存成本相对基准价（元/吨）：" + "；".join(f"{cn[m]} {float(d):+,.0f}" for m, _, _, d in inv) + "。"
          "负数表示库存成本高于基准价，存在跌价风险，需要结合实际现货价判断。",
          "", "## 三、同类公司参照（公开年报摘要，数字待对照原文复核）\n",
          "- 五矿发展 2025 年营业收入 528.23 亿元，同比下降 21.44%；归母净利润 0.19 亿元，同比下降 84.80%。来源：上海证券报 2026-03-31。",
          "- 浙商中拓 2025 年计提信用减值损失 3,837.98 万元，涉及应收票据、应收账款、其他应收款等。来源：上海证券报 2026-04-24。",
          "", "## 四、需要人工确认\n",
          "- 以上不构成价格预测或交易建议；报价请用交易所或行情平台的现货价。",
          "- 同类公司数字抓取自网页摘要，使用前请对照巨潮资讯原文。", ""]
    OUT.mkdir(exist_ok=True)
    (OUT / "BRIEF_SAMPLE.md").write_text("\n".join(b), encoding="utf-8")
    (OUT / "RUN_LOG.md").write_text("\n".join(log) + "\n", encoding="utf-8")
    say("\n已写入 metals_sim_run/RUN_LOG.md 与 BRIEF_SAMPLE.md")


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
