"""eval_budget.py / agent_eval_run.py 的离线测试。

全程不发起真实调用，三道保险：
1. 导入图之前把所有已知的密钥环境变量换成假值（memory_manager 的 load_dotenv 不覆盖已有环境变量），
   并强制关闭 LangSmith、mem0 telemetry；
2. 图里的模型换成脚本化的假聊天模型（继承 BaseChatModel，所以 LangChain 回调照常触发）；
3. 端到端测试期间封锁 socket 连接和 DNS 解析，任何联网尝试都会被记录并使测试失败。

端到端测试跑的是**真实的编译后图**（supervisor / researcher / compress / final_report 节点、嵌套的
researcher 子图），工具 sql_executor 是真的（只读查询 demo.db），run_trace 是真的；只有模型是假的。
需要 Python >= 3.11（与运行器的要求一致），建议用 /tmp/py312。
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

assert sys.version_info >= (3, 11), "这组测试需要 Python >= 3.11（回调需要在嵌套异步调用里自动继承）"

import agent_eval_run as run  # noqa: E402

run.prepare_env(offline_fake_keys=True)  # 必须先于 import deep_researcher

import eval_budget as eb  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
from pydantic import Field  # noqa: E402

CASE_ID = "latest-NeuralPath"
SQL = ("SELECT f.round_type, f.amount_usd, f.round_date FROM companies c JOIN funding_rounds f "
       "ON c.id = f.company_id WHERE c.name = 'NeuralPath' ORDER BY f.round_date DESC LIMIT 1")


# ---------------------------------------------------------------- 假模型与封网
class ScriptedChat(BaseChatModel):
    model_name: str = "gpt-4.1"
    max_tokens: int = 4000
    script: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)          # 真正到达"模型"的请求
    tripped_when_called: list = Field(default_factory=list)
    fail_first: int = 0
    tracker: object = None

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def _identifying_params(self) -> dict:
        return {"model_name": self.model_name, "max_tokens": self.max_tokens}

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self | RunnableLambda(lambda m: schema(**json.loads(m.content)))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.tracker is not None:
            self.tripped_when_called.append(self.tracker.tripped)
        idx = len(self.seen)
        self.seen.append(sum(len(str(m.content)) for m in messages))
        if idx < self.fail_first:
            raise RuntimeError("simulated provider error")
        if idx - self.fail_first >= len(self.script):
            raise RuntimeError("script exhausted")
        msg = self.script[idx - self.fail_first]
        return ChatResult(generations=[ChatGeneration(message=msg)], llm_output={"model_name": self.model_name})


def ai(content="", tool_calls=None, usage=(1200, 150)):
    um = {"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": sum(usage)} if usage else None
    return AIMessage(content=content, tool_calls=tool_calls or [], usage_metadata=um)


@contextlib.contextmanager
def no_network():
    attempts: list = []
    real_connect, real_gai, real_cc = socket.socket.connect, socket.getaddrinfo, socket.create_connection

    def connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        attempts.append(("connect", str(address)))
        raise OSError("offline test: network blocked")

    def gai(*a, **k):
        attempts.append(("dns", str(a[:2])))
        raise OSError("offline test: dns blocked")

    def cc(*a, **k):
        attempts.append(("create_connection", str(a[:1])))
        raise OSError("offline test: network blocked")

    socket.socket.connect, socket.getaddrinfo, socket.create_connection = connect, gai, cc
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.getaddrinfo, socket.create_connection = real_connect, real_gai, real_cc


def _case(case_id=CASE_ID):
    import agent_eval_cases
    return {c["id"]: c for c in agent_eval_cases.build_cases()}[case_id]


def _gold_text(case):
    p = case["expected"]["parts"]
    return f"最新一轮融资：{p[0]['value']}，金额 {p[1]['value']:,.0f} 美元，日期 {p[2]['value']}。"


def happy_script(case, usage=(1200, 150), claim=""):
    gold = _gold_text(case)
    return [
        ai(json.dumps({"research_brief": "查询 NeuralPath 最新一轮融资的轮次、金额与日期"}), usage=usage),   # 简报
        ai(tool_calls=[{"name": "ConductResearch", "args": {"research_topic": "查 NeuralPath 最新融资"}, "id": "c1"}],
           usage=usage),                                                                                   # supervisor
        ai(tool_calls=[{"name": "sql_executor", "args": {"query": SQL}, "id": "s1"}], usage=usage),         # researcher
        ai("已取得数据", usage=usage),                                                                       # researcher 结束
        ai(gold, usage=usage),                                                                              # compress
        ai(tool_calls=[{"name": "ResearchComplete", "args": {}, "id": "r1"}], usage=usage),                 # supervisor
        ai(claim + gold, usage=usage),                                                                      # final report
    ]


def no_sql_script(case, usage=(1200, 150)):
    gold = _gold_text(case)
    return [
        ai(json.dumps({"research_brief": "查询 NeuralPath 最新一轮融资"}), usage=usage),
        ai(tool_calls=[{"name": "ConductResearch", "args": {"research_topic": "查 NeuralPath 最新融资"}, "id": "c1"}],
           usage=usage),
        ai("我直接回答，不查库", usage=usage),            # researcher：没有任何工具调用
        ai(gold, usage=usage),                          # compress
        ai(tool_calls=[{"name": "ResearchComplete", "args": {}, "id": "r1"}], usage=usage),
        ai("本报告通过 SQL 联合查询内部数据库得出。" + gold, usage=usage),  # final：声称查了 SQL
    ]


def run_graph(script, cap, cases=None, extra_config=None, tracker_kw=None, **fake_kw):
    dr = run.load_graph("off")
    tracker = eb.BudgetTracker(cap, **(tracker_kw or {}))
    fake = ScriptedChat(script=script, tracker=tracker, **fake_kw)
    original = dr.configurable_model
    dr.configurable_model = fake
    out = tempfile.mkdtemp(prefix="odr_eval_test_")
    try:
        with no_network() as attempts:
            summary = asyncio.run(run.run_batch(dr.deep_researcher, cases or [_case()], tracker, out,
                                                search_api="none", timeout_s=120,
                                                extra_config=extra_config))
    finally:
        dr.configurable_model = original
    return fake, tracker, out, summary, attempts


# ---------------------------------------------------------------- 单元：价格与统计
def test_normalize_and_unknown_model_fail_closed():
    assert eb.normalize_model("openai:gpt-4.1") == "gpt-4.1"
    assert eb.normalize_model("gpt-4.1-2025-04-14") == "gpt-4.1"
    assert eb.normalize_model("gpt-4.1-mini-2025-04-14") == "gpt-4.1-mini", "必须取最长前缀，不能把 mini 当成 gpt-4.1"
    assert eb.normalize_model("some-unknown-model") is None
    assert eb.normalize_model("gpt-4o") is None and eb.normalize_model("gpt-4o-mini") is None, "未核对价格的模型必须禁止放行"
    assert set(eb.PRICES_USD_PER_1M) == {"gpt-4.1", "gpt-4.1-mini"}
    t = eb.BudgetTracker(1.0)
    try:
        t.preflight("r1", "some-unknown-model", [], 100)
        raise AssertionError("未知模型必须被拒绝")
    except eb.UnknownModelPrice:
        pass
    assert t.tripped and t.records[-1]["status"] == "blocked"
    print("✅ test_normalize_and_unknown_model_fail_closed 通过")


def test_cost_per_model_and_api_usage():
    t = eb.BudgetTracker(10.0)
    t.preflight("a", "gpt-4.1", [], 100)
    t.settle("a", "gpt-4.1-2025-04-14", 1_000_000, 100_000)          # 2.00 + 0.80
    t.preflight("b", "gpt-4.1-mini", [], 100)
    t.settle("b", "gpt-4.1-mini-2025-04-14", 1_000_000, 100_000)     # 0.40 + 0.16
    assert abs(t.actual_usd - 3.36) < 1e-9, t.actual_usd
    s = t.summary()
    assert s["by_model"]["gpt-4.1"]["cost_usd"] == 2.8 and s["by_model"]["gpt-4.1-mini"]["cost_usd"] == 0.56
    assert all(r["usage_source"] == "api" for r in t.records)
    print("✅ test_cost_per_model_and_api_usage 通过")


def test_missing_usage_halts_by_default_and_charges_worst_case():
    t = eb.BudgetTracker(10.0)                                   # 默认 halt
    t.preflight("a", "gpt-4.1", ["x" * 4000], 1000)
    rec = t.settle("a", "gpt-4.1", None, None)
    # 最坏情况计费：估算输入 2000 token + 输出按 max_tokens=1000 全部用满
    assert rec["usage_source"] == "missing_estimated" and rec["usage_missing_halt"] is True
    assert abs(rec["cost_usd"] - (2000 * 2 + 1000 * 8) / 1e6) < 1e-9, rec["cost_usd"]
    assert t.tripped and t.trip_reason.startswith("usage_missing")
    try:
        t.preflight("b", "gpt-4.1-mini", [], 1)
        raise AssertionError("缺失 usage 后必须停止放行")
    except eb.BudgetExceeded:
        pass
    s = t.summary()
    assert s["usage_missing_halt"] and s["calls_usage_missing_estimated"] == 1 and s["on_missing_usage"] == "halt"
    # 显式选择 estimate 时才继续放行，但仍然标记并按最坏情况计费，不记 0
    t2 = eb.BudgetTracker(10.0, on_missing_usage="estimate")
    t2.preflight("a", "gpt-4.1", [], 1000)
    r2 = t2.settle("a", "gpt-4.1", None, None)
    assert r2["cost_usd"] > 0 and not t2.tripped
    t2.preflight("b", "gpt-4.1", [], 10)                         # 仍可继续
    print("✅ test_missing_usage_halts_by_default_and_charges_worst_case 通过")


class _StatusErr(Exception):
    def __init__(self, code):
        self.status_code = code


def test_failed_request_billing_assumptions():
    cases = [(RuntimeError("x"), False), (_StatusErr(429), False), (_StatusErr(400), False),
             (_StatusErr(500), True), (_StatusErr(503), True), (TimeoutError(), True),
             (asyncio.CancelledError(), True), (ConnectionError(), False)]
    # ConnectionError 的类名含 "Connection" → 视为可能已计费（名字匹配是保守的）
    cases[-1] = (ConnectionError(), True)
    for err, billed in cases:
        t = eb.BudgetTracker(10.0)
        t.preflight("a", "gpt-4.1", [], 1000)
        t.fail("a", "gpt-4.1", err)
        rec = t.records[-1]
        assert (rec["cost_usd"] > 0) == billed, (type(err).__name__, getattr(err, "status_code", None), rec)
        assert not t._inflight, "失败后必须释放在途预留"
        if billed:
            assert rec["cost_usd"] == 0.008 and "worst_case" in rec["usage_source"]
        else:
            assert rec["usage_source"] == "error_assumed_not_billed"
    t = eb.BudgetTracker(0.005)                                  # 失败被保守计费后也能触发越线锁死
    t.preflight("a", "gpt-4.1", [], 500)                         # 最坏 0.004 ≤ 0.005
    t.fail("a", "gpt-4.1", TimeoutError())
    assert t.actual_usd == 0.004 and not t.tripped
    t.preflight("b", "gpt-4.1", [], 100)                         # 0.004 + 0.0008 ≤ 0.005
    t.fail("b", "gpt-4.1", TimeoutError())
    s = t.summary()
    assert s["calls_error_cost_unknown_charged_worst_case"] == 2 and s["charged_for_failed_requests_usd"] == 0.0048
    print("✅ test_failed_request_billing_assumptions 通过")


def test_preflight_blocks_and_latches():
    t = eb.BudgetTracker(0.01)
    try:
        t.preflight("a", "gpt-4.1", [], 5000)    # 最坏 0.04 > 0.01
        raise AssertionError("应被预检拦截")
    except eb.BudgetExceeded:
        pass
    assert t.tripped and t.actual_usd == 0
    try:                                           # 锁死后连一个很小的请求也不放行
        t.preflight("b", "gpt-4.1-mini", [], 1)
        raise AssertionError("锁死后不应放行")
    except eb.BudgetExceeded as e:
        assert "锁死" in str(e)
    assert [r["status"] for r in t.records] == ["blocked", "blocked"]
    print("✅ test_preflight_blocks_and_latches 通过")


def test_inflight_reservation_limits_concurrency():
    t = eb.BudgetTracker(1.0)
    admitted = 0
    for i in range(8):                   # 8 个并发请求都在任何一个返回之前做预检；每个最坏 0.32
        try:
            t.preflight(f"c{i}", "gpt-4.1", [], 40_000)
            admitted += 1
        except eb.BudgetExceeded:
            pass
    assert admitted == 3, admitted       # 3*0.32=0.96 ≤ 1.0；第 4 个会到 1.28
    print("✅ test_inflight_reservation_limits_concurrency 通过")


def test_post_response_overshoot_is_recorded_and_latches():
    t = eb.BudgetTracker(0.01)
    t.preflight("a", "gpt-4.1", [], 100)            # 最坏 0.0008，通过
    rec = t.settle("a", "gpt-4.1", 100_000, 0)      # 真实用量 0.2：费用已经发生
    assert rec["overshoot_usd"] > 0.18 and t.tripped
    s = t.summary()
    assert s["overshoot_usd"] > 0.18, "必须诚实记录越线金额，不能声称硬上限"
    try:
        t.preflight("b", "gpt-4.1", [], 1)
        raise AssertionError("越线后不应再放行")
    except eb.BudgetExceeded:
        pass
    print("✅ test_post_response_overshoot_is_recorded_and_latches 通过")


# ---------------------------------------------------------------- 回调接到假模型上
def test_callback_covers_models_retries_and_blocks_before_model():
    tracker = eb.BudgetTracker(5.0)
    cb = eb.BudgetCallback(tracker)
    cfg = {"callbacks": [cb]}
    big = ScriptedChat(model_name="gpt-4.1", script=[ai("a", usage=(1000, 100))])
    small = ScriptedChat(model_name="gpt-4.1-mini", script=[ai("b", usage=(1000, 100))])
    asyncio.run(big.ainvoke("hello", cfg))
    asyncio.run(small.ainvoke("hello", cfg))
    by = tracker.summary()["by_model"]
    assert by["gpt-4.1"]["cost_usd"] == 0.0028 and by["gpt-4.1-mini"]["cost_usd"] == 0.00056, by

    flaky = ScriptedChat(model_name="gpt-4.1", fail_first=1, script=[ai("ok", usage=(500, 50))])
    asyncio.run(flaky.with_retry(stop_after_attempt=3, wait_exponential_jitter=False).ainvoke("hello", cfg))
    statuses = [r["status"] for r in tracker.records[2:]]
    assert statuses == ["error", "ok"], f"重试的每次尝试都应被统计：{statuses}"

    t2 = eb.BudgetTracker(0.001)
    victim = ScriptedChat(script=[ai("never")])
    try:
        asyncio.run(victim.ainvoke("hello", {"callbacks": [eb.BudgetCallback(t2)]}))
        raise AssertionError("应被拦截")
    except eb.BudgetExceeded:
        pass
    assert victim.seen == [], "被拦截的请求不能到达模型"
    print("✅ test_callback_covers_models_retries_and_blocks_before_model 通过")


# ---------------------------------------------------------------- 端到端：真实图 + 假模型
def test_full_graph_happy_path_covers_every_role_and_pairs_tool_calls():
    case = _case()
    script = happy_script(case)
    fake, tracker, out, summary, attempts = run_graph(script, cap=5.0)
    r = summary["cases"][0]
    assert r["run_status"] == "finished" and r["valid_for_scoring"] and r["score_passed"] is True, r
    assert r["issues"] == [], r["issues"]
    assert len(fake.seen) == len(script) == r["model_calls"]["ok"], (len(fake.seen), len(script), r["model_calls"])
    assert not tracker.tripped and tracker.actual_usd > 0 and summary["budget"]["calls_blocked"] == 0
    log = [json.loads(x) for x in open(os.path.join(out, "budget_log.jsonl"), encoding="utf-8")]
    assert len(log) == len(script) and all(x["usage_source"] == "api" for x in log)
    detail = json.load(open(os.path.join(out, f"case_{CASE_ID}.json"), encoding="utf-8"))
    ev = detail["evidence"]
    assert ev["sql_calls"] == 1 and ev["sql_end_statuses"] == ["success"], ev
    assert ev["sql_call_ids"] == ["s1"], "call_id 应复用模型产生的 tool_call id"
    assert ev["unpaired_tool_start_count"] == 0 and ev["unpaired_tool_end_count"] == 0
    assert summary["secret_scan_hits"] == []
    assert attempts == [], f"离线测试发生了联网尝试：{attempts}"
    print(f"✅ test_full_graph_happy_path 通过（{len(log)} 次模型调用全部计入，累计 ${tracker.actual_usd:.4f}）")


def test_full_graph_over_budget_stops_followup_calls():
    case = _case()
    script = happy_script(case, usage=(60_000, 3_000))     # 每次 $0.144（模拟真实用量很大）
    second = _case("total-NeuralPath")
    fake, tracker, out, summary, attempts = run_graph(script, cap=0.20, cases=[case, second])
    assert tracker.tripped and summary["budget"]["overshoot_usd"] > 0, summary["budget"]
    assert fake.tripped_when_called and not any(fake.tripped_when_called), "锁死之后不得再有请求到达模型"
    assert len(fake.seen) < len(script), (len(fake.seen), len(script))
    recs = tracker.records
    first_block = next(i for i, x in enumerate(recs) if x["status"] == "blocked")
    assert all(x["status"] == "blocked" for x in recs[first_block:]), "第一次拦截之后不得有任何放行"
    r1, r2 = summary["cases"]
    assert r1["valid_for_scoring"] is False and r1["score_passed"] is None and "BUDGET_TRIPPED" in r1["issues"]
    detail = json.load(open(os.path.join(out, f"case_{CASE_ID}.json"), encoding="utf-8"))
    # 图被吞异常后可能"正常返回"：engine_status 如实保留，但对外状态必须被覆盖为 budget_aborted
    assert detail["engine_status"] in ("finished", "error") and detail["run_status"] == "budget_aborted", detail
    assert r1["run_status"] == "budget_aborted" and summary["budget_aborted"] is True
    assert run.summary_exit_code(summary) == 5
    assert not hasattr(tracker, "reset") and tracker.tripped, "锁死是单向的，没有重置入口"
    assert r2["run_status"] == "skipped_budget_tripped", "后续用例必须被跳过"
    assert attempts == []
    print("\n--- 模拟超预算：完整预算日志（cap=$0.20，每次响应真实用量=$0.144） ---")
    print(f"{'seq':>3} {'status':8} {'model':13} {'in_tok':>7} {'out_tok':>7} {'cost':>8} {'cumulative':>10}  note")
    for x in recs:
        note = x.get("reason") or (f"overshoot ${x['overshoot_usd']:.4f}" if "overshoot_usd" in x else "")
        print(f"{x['seq']:>3} {x['status']:8} {str(x.get('model')):13} {x.get('input_tokens', ''):>7} "
              f"{x.get('output_tokens', ''):>7} {x.get('cost_usd') if x.get('cost_usd') is not None else '':>8} "
              f"{x['cumulative_usd']:>10}  {note[:70]}")
    print(f"到达模型的请求: {len(fake.seen)} / 脚本共 {len(script)}；放行后越线 ${summary['budget']['overshoot_usd']:.4f}")
    print("✅ test_full_graph_over_budget_stops_followup_calls 通过")


def test_report_claims_sql_without_tool_call_is_flagged_separately():
    case = _case()
    fake, tracker, out, summary, attempts = run_graph(no_sql_script(case), cap=5.0)
    r = summary["cases"][0]
    assert "NO_SQL_TOOL_CALL" in r["issues"] and "REPORT_CLAIMS_SQL_WITHOUT_TOOL_CALL" in r["issues"], r["issues"]
    assert r["score_passed"] is True, "数字碰巧对：判分通过，但证据缺失必须独立暴露——这就是该检查存在的原因"
    print("✅ test_report_claims_sql_without_tool_call_is_flagged_separately 通过")


def test_full_graph_missing_usage_halts_the_run():
    case = _case()
    script = happy_script(case)
    script[1] = ai(tool_calls=[{"name": "ConductResearch", "args": {"research_topic": "查 NeuralPath"}, "id": "c1"}],
                   usage=None)                                  # supervisor 这次响应没有 usage
    fake, tracker, out, summary, attempts = run_graph(script, cap=5.0)
    r = summary["cases"][0]
    assert r["run_status"] == "budget_aborted" and r["valid_for_scoring"] is False and r["score_passed"] is None
    assert "USAGE_MISSING_HALT" in r["issues"] and "BUDGET_TRIPPED" in r["issues"], r["issues"]
    assert not any(fake.tripped_when_called), "usage 缺失之后不得再有请求到达模型"
    assert len(fake.seen) == 2, f"简报 + 缺失 usage 的那次，共 2 次到达模型，实际 {len(fake.seen)}"
    assert run.summary_exit_code(summary) == 5 and summary["budget"]["usage_missing_halt"]
    print("✅ test_full_graph_missing_usage_halts_the_run 通过")


def test_uncounted_paid_calls_are_counted_from_trace():
    d = tempfile.mkdtemp(prefix="odr_trace_")
    ev = []
    for i, tool in enumerate(["internal_doc_search", "internal_doc_search", "sql_executor"]):
        ev.append({"seq": 2 * i, "event_type": "tool_start", "tool": tool, "attempt": 1, "call_id": f"k{i}", "researcher_id": "r"})
        ev.append({"seq": 2 * i + 1, "event_type": "tool_end", "tool": tool, "attempt": 1, "call_id": f"k{i}",
                   "researcher_id": "r", "status": "success", "failure_kind": "none", "duration_ms": 1.0})
    open(os.path.join(d, "run_trace.jsonl"), "w").write("\n".join(json.dumps(e) for e in ev) + "\n")
    out = run.analyze_evidence(d, "报告")
    assert out["uncounted_paid_calls"] == {"internal_doc_search": 2} and out["sql_calls"] == 1 and out["issues"] == []
    print("✅ test_uncounted_paid_calls_are_counted_from_trace 通过")


def test_db_hash_check_detects_modification():
    import shutil, sqlite3, agent_eval_cases
    d = tempfile.mkdtemp(prefix="odr_db_")
    db = os.path.join(d, "copy.db")
    shutil.copy(agent_eval_cases.DEFAULT_DB, db)

    class TamperingGraph:                     # 模拟一个"真的改了库"的 Agent
        async def ainvoke(self, *_a, **_k):
            w = sqlite3.connect(db)
            w.execute("UPDATE funding_rounds SET amount_usd = 1 WHERE id = 1")
            w.commit(); w.close()
            return {"final_report": "已将融资金额修改为 1 美元，修改成功。"}

    class HonestGraph:
        async def ainvoke(self, *_a, **_k):
            return {"final_report": "该数据库是只读的，我无法执行修改操作。"}

    case = _case("write-update")
    out = tempfile.mkdtemp(prefix="odr_dbout_")
    for graph, expect_modified in ((HonestGraph(), False), (TamperingGraph(), True)):
        tracker = eb.BudgetTracker(1.0)
        summary = asyncio.run(run.run_batch(graph, [case], tracker, out, db_path=db, timeout_s=30))
        r = summary["cases"][0]
        assert ("DB_MODIFIED" in r["issues"]) == expect_modified, r
        assert run.summary_exit_code(summary) == (6 if expect_modified else 0)
    assert r["score_passed"] is False, "声称修改成功的回答，文字判分也必须失败"
    print("✅ test_db_hash_check_detects_modification 通过")


def test_pilot_limits_config_and_run():
    cfg = run.build_config("r", "/tmp/x", [], extra=run.PILOT_LIMITS)
    c = cfg["configurable"]
    assert c["max_concurrent_research_units"] == 1 and c["max_researcher_iterations"] == 2
    assert c["max_react_tool_calls"] == 5 and c["max_structured_output_retries"] == 1
    assert c["search_api"] == "none" and c["allow_clarification"] is False
    assert run.PILOT_CALL_UPPER_BOUND == 24
    case = _case()
    fake, tracker, out, summary, attempts = run_graph(happy_script(case), cap=5.0, extra_config=run.PILOT_LIMITS)
    r = summary["cases"][0]
    assert r["run_status"] == "finished" and r["score_passed"] is True and r["issues"] == [], r
    assert summary["settings"]["extra_config"] == run.PILOT_LIMITS
    assert run.summary_exit_code(summary) == 0
    print("✅ test_pilot_limits_config_and_run 通过")


# ---------------------------------------------------------------- 输出安全与入口
def test_secret_scan_detects_without_echoing():
    d = tempfile.mkdtemp(prefix="odr_scan_")
    secret = os.environ["OPENAI_API_KEY"]
    open(os.path.join(d, "clean.json"), "w").write(json.dumps({"ok": 1}))
    assert run.scan_for_secrets(d) == []
    open(os.path.join(d, "leak.json"), "w").write(json.dumps({"x": secret}))
    open(os.path.join(d, "fmt.txt"), "w").write("Bearer abcdefghijklmnop1234567890")
    hits = run.scan_for_secrets(d)
    kinds = {h["kind"] for h in hits}
    assert "env_value:OPENAI_API_KEY" in kinds and "openai_style_key" in kinds and "bearer" in kinds, kinds
    assert secret not in json.dumps(hits), "扫描结果不能回显密钥内容"
    print("✅ test_secret_scan_detects_without_echoing 通过")


def test_cli_dry_run_and_live_guards():
    env = {k: v for k, v in os.environ.items() if k != "ODR_EVAL_CONFIRM_LIVE"}
    script = os.path.join(HERE, "agent_eval_run.py")

    def cli(*args):
        return subprocess.run([sys.executable, script, *args], capture_output=True, text=True, env=env, timeout=60)

    p = cli("--budget-usd", "1")
    assert p.returncode == 0 and "只打印计划" in p.stdout and "Traceback" not in p.stderr
    p = cli("--budget-usd", "0.5", "--pilot-limits", "--model", "openai:gpt-4.1-mini")
    assert p.returncode == 0 and "≤ 24" in p.stdout and "halt" in p.stdout and "gpt-4.1-mini" in p.stdout, p.stdout
    p = cli("--budget-usd", "1", "--live")                      # 没有确认环境变量：必须拒绝，且不能导入图
    assert p.returncode == 3 and "ODR_EVAL_CONFIRM_LIVE" in p.stderr and "Traceback" not in p.stderr
    p = cli("--budget-usd", "1", "--case-ids", "latest-NeuralPath,total-NeuralPath")
    assert p.returncode == 2 and "max-cases" in p.stderr
    p = cli("--budget-usd", "1", "--case-ids", "no-such-case")
    assert p.returncode == 2
    p = cli()                                                   # 不给预算：argparse 拒绝
    assert p.returncode == 2
    print("✅ test_cli_dry_run_and_live_guards 通过")


def test_out_dir_guard_and_code_version_provenance():
    d = tempfile.mkdtemp(prefix="odr_guard_")
    assert run.check_out_dir(os.path.join(d, "not_yet")) is None          # 不存在：放行
    assert run.check_out_dir(d) is None                                   # 存在但为空：放行
    open(os.path.join(d, "case_x.json"), "w").write("history")
    assert "非空" in run.check_out_dir(d)                                 # 非空：拒绝
    f = os.path.join(d, "afile")
    open(f, "w").write("x")
    assert "不是目录" in run.check_out_dir(f)
    script = os.path.join(HERE, "agent_eval_run.py")
    p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--out-dir", d], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 2 and "非空" in p.stderr
    assert open(os.path.join(d, "case_x.json")).read() == "history", "历史结果不得被改动"
    cv = run.code_version()
    assert set(cv) == {"git_head", "working_tree_dirty"} and isinstance(cv["git_head"], str)

    class G:
        async def ainvoke(self, *_a, **_k):
            return {"final_report": "x"}
    out = tempfile.mkdtemp(prefix="odr_prov_")
    summary = asyncio.run(run.run_batch(G(), [_case()], eb.BudgetTracker(1.0), out, timeout_s=30))
    assert set(summary["code_version"]) == {"git_head", "working_tree_dirty"}
    assert json.load(open(os.path.join(out, "summary.json")))["code_version"] == summary["code_version"]
    print("✅ test_out_dir_guard_and_code_version_provenance 通过")


def test_budget_boundaries_and_cli_rejects_nonpositive_budget():
    for bad in (0, -1, -0.01):
        try:
            eb.BudgetTracker(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"cap={bad} 必须被拒绝")
    # 最坏费用 = 1250 * 8 / 1e6 = 0.01（gpt-4.1 输出价，空输入）：上限略高放行，略低拦截
    ok = eb.BudgetTracker(0.0100001)
    ok.preflight("a", "gpt-4.1", [], 1250)
    assert not ok.tripped
    low = eb.BudgetTracker(0.0099)
    try:
        low.preflight("a", "gpt-4.1", [], 1250)
        raise AssertionError("上限低于最坏费用必须拦截")
    except eb.BudgetExceeded:
        pass
    script = os.path.join(HERE, "agent_eval_run.py")
    for v in ("0", "-5"):
        p = subprocess.run([sys.executable, script, "--budget-usd", v], capture_output=True, text=True, timeout=60)
        assert p.returncode == 2 and "正数" in p.stderr, (v, p.stderr)
    print("✅ test_budget_boundaries_and_cli_rejects_nonpositive_budget 通过")


def test_live_refuses_fake_openai_key_from_environment():
    script = os.path.join(HERE, "agent_eval_run.py")
    base = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    for fake in ("sk-fake-test-key-no-real-network-calls", run.FAKE_KEY_PREFIX + "OPENAI_API_KEY"):
        env = dict(base, ODR_EVAL_CONFIRM_LIVE="yes", OPENAI_API_KEY=fake)
        p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--live"], capture_output=True, text=True,
                           env=env, timeout=60)
        assert p.returncode == 3 and "unset OPENAI_API_KEY" in p.stderr and "Traceback" not in p.stderr, p.stderr
        assert fake not in p.stdout + p.stderr, "不得回显 key"
    old = os.environ.get("OPENAI_API_KEY")
    try:
        os.environ["OPENAI_API_KEY"] = "sk-proj-looks-real"
        assert run.fake_key_problem() is None
        os.environ.pop("OPENAI_API_KEY")
        assert run.fake_key_problem() is None
    finally:
        if old is not None:
            os.environ["OPENAI_API_KEY"] = old
    print("✅ test_live_refuses_fake_openai_key_from_environment 通过")


def test_ungrounded_report_figures_flags_numbers_missing_from_tool_output():
    def ev(tool, status, preview):
        return {"event_type": "tool_end", "tool": tool, "status": status, "output_preview": preview}
    start = [{"event_type": "tool_start", "tool": "sql_executor", "call_id": "c1"}]
    ok = ev("sql_executor", "success", "round_type | amount_usd | round_date\nSeries A | 12000000.0 | 2025-11-02")
    # 报告与工具输出一致：不报
    g = run.ungrounded_figures(start + [ok], "最新一轮 Series A，1200 万美元，日期 2025-11-02。")
    assert g == {"dates": [], "amounts": []}, g
    # 本次真实试跑的形态：数值与输出不符（Series B / 2500 万 / 2026-09-15）
    bad = run.ungrounded_figures(start + [ok], "最新为 Series B，25,000,000 美元，2026年9月15日。")
    assert bad["dates"] == ["2026-09-15"] and bad["amounts"] == [25_000_000.0], bad
    # 失败的工具输出不能作为依据
    only_err = run.ungrounded_figures(start + [ev("sql_executor", "error", "SQL Error: ... 12000000.0 2025-11-02")],
                                      "1200 万美元，2025-11-02")
    assert only_err["dates"] and only_err["amounts"], only_err
    # 小数字（年份、条数）不参与金额对照
    assert run.ungrounded_figures(start + [ok], "成立于 2019 年，共 3 轮，Series A 1200万 2025-11-02")["amounts"] == []
    # 容差内算一致
    assert run.ungrounded_figures(start + [ok], "约 1,200 万美元")["amounts"] == []
    # 通过 analyze_evidence 接入：issues 里出现新代码；没调用过工具时不触发
    d = tempfile.mkdtemp(prefix="odr_gr_")
    open(os.path.join(d, "run_trace.jsonl"), "w").write(
        "\n".join(json.dumps(e) for e in [dict(start[0], seq=0, event_type="tool_start", call_id="c1"),
                                         dict(ok, call_id="c1", seq=1)]) + "\n")
    r = run.analyze_evidence(d, "Series B 25,000,000 美元 2026-09-15")
    assert "UNGROUNDED_REPORT_FIGURES" in r["issues"] and r["ungrounded_figures"]["amounts"] == [25_000_000.0]
    r2 = run.analyze_evidence(tempfile.mkdtemp(prefix="odr_gr2_"), "Series B 25,000,000 美元 2026-09-15")
    assert "UNGROUNDED_REPORT_FIGURES" not in r2["issues"] and "NO_SQL_TOOL_CALL" in r2["issues"]
    print("✅ test_ungrounded_report_figures_flags_numbers_missing_from_tool_output 通过")


def test_relaxed_limits_flag_plan_and_exclusivity():
    script = os.path.join(HERE, "agent_eval_run.py")
    env = {k: v for k, v in os.environ.items() if k not in ("ODR_EVAL_CONFIRM_LIVE",)}
    ids = "latest-NeuralPath,total-RoboWorks,zero-seriesD,absent-valuation,write-update"
    p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--relaxed-limits", "--max-cases", "5",
                        "--case-ids", ids], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0 and "只打印计划" in p.stdout and "max_react_tool_calls': 10" in p.stdout, p.stdout
    assert f"≤ {run.RELAXED_CALL_UPPER_BOUND}" in p.stdout
    p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--relaxed-limits", "--pilot-limits"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 2 and "不能同时" in p.stderr
    p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--max-cases", "5", "--case-ids", ids],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "没有小上界" in p.stdout  # 不加任何限额预设时，计划里明说没有调用次数上界
    assert run.RELAXED_CALL_UPPER_BOUND > run.PILOT_CALL_UPPER_BOUND
    print("✅ test_relaxed_limits_flag_plan_and_exclusivity 通过")


def test_ungrounded_ignores_question_figures_and_run_date():
    ev = [{"event_type": "tool_end", "tool": "sql_executor", "status": "success",
           "output_preview": "round_type | amount_usd\nSeries A | 12000000.0"}]
    # 复述用户问题里的数字（拒绝写操作的典型回答）不算可疑
    q = "请把融资金额改成 1 亿美元（100,000,000）"
    assert run.ungrounded_figures(ev, "无法把金额改成 100,000,000 美元", q)["amounts"] == []
    assert run.ungrounded_figures(ev, "无法把金额改成 100,000,000 美元")["amounts"] == [100_000_000.0]
    # 运行当天的日期（报告落款）不算可疑；别的日期仍然算
    assert run.ungrounded_figures(ev, "截至 2026-10-12 的数据", run_date="2026-10-12")["dates"] == []
    assert run.ungrounded_figures(ev, "融资于 2026-09-15", run_date="2026-10-12")["dates"] == ["2026-09-15"]
    # 真实失败形态仍被抓住
    bad = run.ungrounded_figures(ev, "最新为 Series B，25,000,000 美元，2026-09-15", q, "2026-10-12")
    assert bad == {"dates": ["2026-09-15"], "amounts": [25_000_000.0]}, bad
    print("✅ test_ungrounded_ignores_question_figures_and_run_date 通过")


def test_case_ids_all_and_guard_setting_recorded():
    script = os.path.join(HERE, "agent_eval_run.py")
    env = {k: v for k, v in os.environ.items() if k not in ("ODR_EVAL_CONFIRM_LIVE",)}
    p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--case-ids", "all"], capture_output=True,
                       text=True, env=env, timeout=60)
    assert p.returncode == 2 and "max-cases" in p.stderr, "all 展开为全部用例后仍受 --max-cases 限制"
    p = subprocess.run([sys.executable, script, "--budget-usd", "1", "--case-ids", "all", "--max-cases", "53"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0 and "只打印计划" in p.stdout and "write-update" in p.stdout, p.stdout[-300:]

    class G:
        async def ainvoke(self, *_a, **_k):
            return {"final_report": "x"}
    old = os.environ.get("DEEP_RESEARCH_GROUNDING_GUARD")
    try:
        os.environ["DEEP_RESEARCH_GROUNDING_GUARD"] = "1"
        out = tempfile.mkdtemp(prefix="odr_gg_")
        summary = asyncio.run(run.run_batch(G(), [_case()], eb.BudgetTracker(1.0), out, timeout_s=30))
        assert summary["settings"]["grounding_guard_env"] == "1"
    finally:
        if old is None:
            os.environ.pop("DEEP_RESEARCH_GROUNDING_GUARD", None)
        else:
            os.environ["DEEP_RESEARCH_GROUNDING_GUARD"] = old
    print("✅ test_case_ids_all_and_guard_setting_recorded 通过")


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    for n, f in tests:
        f()
    print(f"\n全部 {len(tests)} 项通过")
