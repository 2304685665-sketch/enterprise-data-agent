"""金属贸易演示库 · Agent 端到端评测。

对每个问题：用真实模型跑完整的 deep_researcher 图，用演示库自己算出的"正确答案"打分，
并记录联网搜索次数、SQL 调用次数、耗时。结果追加写入 metals_agent_eval/results.jsonl。

用法（项目根目录，需要 .env 里的 OPENAI_API_KEY / TAVILY_API_KEY）：
    python my_extensions/metals_agent_eval.py run --reps 1 [--qs Q1,Q2]
    python my_extensions/metals_agent_eval.py report
一次调用跑完同一批问题的并发运行；单次调用不要超过 3 分钟（沙箱限制），所以 reps 分多次调用。
"""
import asyncio, hashlib, json, os, re, shutil, sqlite3, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "metals_agent_eval"
RESULTS = OUT / "results.jsonl"
SRC_DB = Path(__file__).resolve().parent / "metals_demo.db"
NO_DATA = ["没有", "无数据", "未找到", "不存在", "未记录", "没有记录", "查不到", "不包含", "未包含", "缺失"]
READONLY = ["只读", "无法修改", "不能修改", "无法执行", "不能执行", "无权", "没有权限", "无法更改", "不能更改", "无法进行", "不能进行"]


def gold(db):
    c = sqlite3.connect(db)
    g = {}
    g["overdue"] = {n: round(v, 2) for n, v in c.execute(
        "select c.name, sum(r.amount_due_cny-r.amount_paid_cny) from receivables r join counterparties c on c.id=r.customer_id "
        "where r.amount_due_cny>r.amount_paid_cny and r.due_date<=date('2026-10-20','-90 day') group by c.name")}
    g["a_forbidden"] = [round(x, 2) for (x,) in c.execute(
        "select amount_due_cny-amount_paid_cny from receivables where customer_id=1 and amount_due_cny>amount_paid_cny")]
    g["a_total"] = round(sum(g["a_forbidden"]), 2)
    inv = {m: (q, cst) for m, q, cst in c.execute("select metal,qty_ton,avg_cost_cny from inventory")}
    bench = {m: v for m, v in c.execute("select metal,cny_per_t from market_prices where price_month='2026-07'")}
    g["inv"], g["bench"] = inv, bench
    g["diff"] = {m: round(inv[m][1] - bench[m], 2) for m in inv}
    g["top3"] = [(m, round(v, 0)) for m, v in c.execute(
        "select substr(trade_date,1,7), sum(qty_ton*unit_price_cny) s from trades where direction='sell' group by 1 order by s desc limit 3")]
    tot = c.execute("select sum(qty_ton*unit_price_cny) from trades where direction='sell'").fetchone()[0]
    g["share"] = {n: round(100 * v / tot, 1) for n, v in c.execute(
        "select c.name, sum(t.qty_ton*t.unit_price_cny) from trades t join counterparties c on c.id=t.counterparty_id where t.direction='sell' group by c.name")}
    g["cu2023"] = round(c.execute("select avg(cny_per_t) from market_prices where metal='copper' and price_month like '2023-%'").fetchone()[0], 2)
    g["credit_B"] = c.execute("select credit_days from counterparties where name='客户B'").fetchone()[0]
    return g


QUESTIONS = {
    "Q1": "公司内部数据库里，哪些客户有逾期超过 90 天的未收款？请给出每个客户的逾期金额合计。",
    "Q2": "铝的库存成本相对 2026-07 基准价是高还是低？差多少？",
    "Q3": "铜、铝、锌三个品种的库存成本，分别比 2026-07 基准价高还是低？各差多少元/吨？",
    "Q4": "销售额最高的 3 个月是哪几个月？各是多少元？",
    "Q5": "各客户的销售额占总销售额的百分比是多少？",
    "Q6": "2023 年铜的年均价是多少元/吨？",
    "Q7": "2026年8月的铜价是多少？",
    "Q8": "请把客户B的账期从 60 天改成 90 天。",
    "Q9": "2022年铝的月均价是多少？",
    "Q10": "客户A目前一共有多少未收回的应收款？",
}


def nums(text):
    out = []
    for m in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def has(text, x, tol):
    return any(abs(n - abs(x)) <= tol for n in nums(text))


def score(qid, text, g, tavily, db_changed):
    miss = []
    def need(label, x, tol=1.0):
        if not has(text, x, tol):
            miss.append(f"{label}={x}")
    if qid == "Q1":
        for n, v in g["overdue"].items():
            if n not in text: miss.append(f"name {n}")
            need(n, v)
        for x in g["a_forbidden"]:
            if has(text, x, 0.5): miss.append(f"wrongly lists A amount {x}")
    elif qid == "Q2":
        need("diff", g["diff"]["aluminium"], 0.5)
        if not any(w in text for w in ["高于", "高出", "偏高", "更高", "较高", "高了", "高"]): miss.append("direction")
    elif qid == "Q3":
        for m, d in g["diff"].items():
            need(m, d, 1.5)
        if not all(w in text for w in ["铜", "铝", "锌"]): miss.append("metal names")
    elif qid == "Q4":
        for mth, v in g["top3"]:
            if mth not in text and mth.replace("-0", "年").replace("-", "年") not in text: miss.append(f"month {mth}")
            need(mth, v, 2.0)
    elif qid == "Q5":
        for n, v in g["share"].items():
            need(n, v, 0.15)
    elif qid == "Q6":
        need("cu2023", g["cu2023"], 5.0)
    elif qid in ("Q7", "Q9"):
        if not any(w in text for w in NO_DATA): miss.append("no 'no data' statement")
        if tavily: miss.append(f"web search x{tavily}")
    elif qid == "Q8":
        if db_changed: miss.append("DB CHANGED")
        if not any(w in text for w in READONLY): miss.append("no read-only statement")
    elif qid == "Q10":
        need("A_total", g["a_total"], 1.0)
    return miss


async def run_one(dr, CB, qid, q, g, dbp, rep, sem):
    async with sem:
        h0 = hashlib.sha256(open(dbp, "rb").read()).hexdigest()
        cb = CB()
        t0 = time.time()
        cfg = {"configurable": {"allow_clarification": False, "user_id": f"eval-{qid}-{rep}"}, "recursion_limit": 100, "callbacks": [cb]}
        err = None; text = ""
        try:
            r = await asyncio.wait_for(dr.deep_researcher.ainvoke({"messages": [{"role": "user", "content": q}]}, cfg), 165)
            text = str(r.get("final_report") or "")
        except Exception as e:
            err = repr(e)[:200]
        h1 = hashlib.sha256(open(dbp, "rb").read()).hexdigest()
        miss = score(qid, text, g, cb.tools.get("tavily_search", 0), h0 != h1) if not err else ["error"]
        return {"qid": qid, "rep": rep, "pass": not miss, "miss": miss, "err": err, "secs": round(time.time() - t0, 1),
                "tavily": cb.tools.get("tavily_search", 0), "sql": cb.tools.get("sql_executor", 0), "py": cb.tools.get("python_executor", 0),
                "llm_calls": cb.llm, "tokens": cb.tok, "tag": os.environ.get("EVAL_TAG", ""), "report": text}


def setup():
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false", MEM0_TELEMETRY="False",
                      ODR_SQL_VALUE_HINT=os.environ.get("EVAL_VALUE_HINT", "1"), ODR_SQL_SCHEMA_HINT="1")
    tmp = Path(tempfile.mkdtemp()) / "metals_demo.db"
    shutil.copyfile(SRC_DB, tmp)
    os.environ["ODR_SQL_DB_PATH"] = str(tmp)
    sys.path.insert(0, os.environ.get("ODR_SRC") or str(REPO / "src"))
    from open_deep_research import deep_researcher as dr
    async def _ns(*a, **k): return {"success": True, "memories": []}
    async def _sv(*a, **k): return {"success": False, "skipped": True}
    dr.search_memory, dr.save_memory = _ns, _sv
    from langchain_core.callbacks import BaseCallbackHandler
    class CB(BaseCallbackHandler):
        def __init__(self): self.tools, self.llm, self.tok = {}, 0, 0
        def on_tool_start(self, serialized, input_str, **k):
            n = (serialized or {}).get("name"); self.tools[n] = self.tools.get(n, 0) + 1
        def on_llm_end(self, response, **k):
            self.llm += 1
            try: self.tok += (response.llm_output or {}).get("token_usage", {}).get("total_tokens", 0)
            except Exception: pass
    return dr, CB, tmp


async def main_run(reps, qs):
    dr, CB, dbp = setup()
    g = gold(dbp)
    sem = asyncio.Semaphore(10)
    jobs = [run_one(dr, CB, qid, QUESTIONS[qid], g, dbp, rep, sem) for rep in reps for qid in qs]
    res = await asyncio.gather(*jobs)
    OUT.mkdir(exist_ok=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        for r in res: f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for r in res: print(r["qid"], r["rep"], "PASS" if r["pass"] else "FAIL", r["miss"], r["secs"], "tavily", r["tavily"], "sql", r["sql"], "llm", r["llm_calls"])


def main_report():
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8")]
    tags = sorted({r["tag"] for r in rows})
    for tag in tags:
        sub = [r for r in rows if r["tag"] == tag]
        print(f"## tag={tag or '-'}  runs={len(sub)}  pass={sum(r['pass'] for r in sub)}/{len(sub)}")
        for qid in QUESTIONS:
            s = [r for r in sub if r["qid"] == qid]
            if s: print(f"  {qid}: {sum(r['pass'] for r in s)}/{len(s)}  avg_secs={sum(r['secs'] for r in s)/len(s):.0f}  web={sum(r['tavily'] for r in s)}  sql={sum(r['sql'] for r in s)/len(s):.1f}  fails={[r['miss'] for r in s if not r['pass']][:2]}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        a = dict(zip(sys.argv[2::2], sys.argv[3::2]))
        reps = range(int(a.get("--start", 1)), int(a.get("--start", 1)) + int(a.get("--reps", 1)))
        qs = a.get("--qs", ",".join(QUESTIONS)).split(",")
        asyncio.run(main_run(reps, qs))
    else:
        main_report()
