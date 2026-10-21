"""人工审批的离线测试：策略表、代理（内存/Redis(fakeredis)）、接入 execute_tool_safely、服务端点。无模型调用。"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")

import fakeredis.aioredis as fr  # noqa: E402
import httpx  # noqa: E402

from open_deep_research import approval as ap  # noqa: E402
from open_deep_research import deep_researcher as dr  # noqa: E402
from open_deep_research.api_service import Settings, create_app  # noqa: E402


def run(c):
    return asyncio.run(c)


def test_policy_table():
    cases = [
        ("python_executor", {"code": "print(1)"}, ap.REQUIRE),
        ("sql_executor", {"query": "SELECT * FROM companies"}, ap.AUTO),
        ("sql_executor", {"query": "SELECT name, sql FROM sqlite_master WHERE type='table'"}, ap.AUTO),
        ("sql_executor", {"query": "SELECT * FROM companies c JOIN funding_rounds f ON f.company_id=c.id"}, ap.REQUIRE),
        ("sql_executor", {"query": "select * from a, b"}, ap.REQUIRE),  # 逗号式隐式连接：加固后可识别
        ("sql_executor", {"query": "SELECT * FROM companies WHERE name='x from y'"}, ap.AUTO),
        ("sql_executor", {"query": "SELECT * FROM companies -- join funding_rounds"}, ap.AUTO),
        ("sql_executor", {"query": "SELECT * FROM companies UNION SELECT * FROM funding_rounds"}, ap.REQUIRE),
        ("internal_doc_search", {"query": "x"}, ap.AUTO),
        ("tavily_search", {"queries": ["x"]}, ap.AUTO),
    ]
    for tool, args, want in cases:
        assert ap.classify(tool, args) == want, (tool, args)
    print("✅ test_policy_table 通过")


def test_comma_join_and_quoted_identifiers_are_detected():
    # 修复前：FROM a, b 只识别出 {"a"}，带引号的表名识别不出；现在都能识别。仍是正则，不是 SQL 解析器。
    assert ap.referenced_tables("select * from a, b") == {"a", "b"}
    assert ap.referenced_tables("select * from a x, b y where x.i=y.i") == {"a", "b"}
    assert ap.referenced_tables('select * from "a" c join "b" f on 1=1') == {"a", "b"}
    assert ap.referenced_tables("select * from a where id in (select id from b)") == {"a", "b"}
    assert ap.referenced_tables("select 'from x, y' as t from a") == {"a"}
    assert ap.classify("sql_executor", {"query": "select * from a, b"}) == ap.REQUIRE
    print("✅ test_comma_join_and_quoted_identifiers_are_detected 通过")


def test_gate_auto_no_broker_and_decisions():
    ap.set_broker(None)
    assert run(ap.gate("internal_doc_search", {}, 1)) == (True, "auto")
    assert run(ap.gate("python_executor", {"code": "x"}, 1)) == (False, "no_broker")

    async def flow(decision_fn):
        b = ap.InMemoryApprovalBroker()
        ap.set_broker(b)
        t = asyncio.create_task(ap.gate("python_executor", {"code": "x"}, 1.0))
        await asyncio.sleep(0.05)
        pend = b.list_pending()
        assert len(pend) == 1 and "python_executor" in pend[0]["summary"]
        decision_fn(b, pend[0]["id"])
        return await t

    assert run(flow(lambda b, i: b.decide(i, True))) == (True, "approved")
    assert run(flow(lambda b, i: b.decide(i, False))) == (False, "denied")
    ap.set_broker(ap.InMemoryApprovalBroker())
    assert run(ap.gate("python_executor", {"code": "x"}, 0.05)) == (False, "timeout")
    ap.set_broker(None)
    print("✅ test_gate_auto_no_broker_and_decisions 通过")


def test_in_memory_first_decision_wins_and_unknown_id():
    async def flow():
        b = ap.InMemoryApprovalBroker()
        t = asyncio.create_task(b.request("s", 1.0))
        await asyncio.sleep(0.02)
        rid = b.list_pending()[0]["id"]
        assert b.decide(rid, True) is True and b.decide(rid, False) is False
        assert await t == ap.APPROVED
        assert b.decide("nope", True) is False
    run(flow())
    print("✅ test_in_memory_first_decision_wins_and_unknown_id 通过")


def test_redis_broker_approve_deny_timeout_and_late_approval_ignored():
    async def flow():
        r = fr.FakeRedis()
        b = ap.RedisApprovalBroker(r, poll_interval=0.01)
        t = asyncio.create_task(b.request("python_executor: {}", 1.0))
        await asyncio.sleep(0.05)
        pend = await b.list_pending()
        assert len(pend) == 1
        assert await b.decide(pend[0]["id"], True) is True and await b.decide(pend[0]["id"], False) is False
        assert await t == ap.APPROVED
        # 超时后迟到的批准无效
        t2 = asyncio.create_task(b.request("x", 0.05))
        await asyncio.sleep(0.01)
        rid = (await b.list_pending())[0]["id"]
        assert await t2 == ap.TIMEOUT
        assert await b.decide(rid, True) is False
        assert await b.decide("missing", True) is False
    run(flow())
    print("✅ test_redis_broker_approve_deny_timeout_and_late_approval_ignored 通过")


class SpyTool:
    name = "python_executor"

    def __init__(self):
        self.calls = 0


def _execute(tool_name, args, enabled, broker, decision=None):
    spy = {"n": 0}

    async def raising(**kw):
        spy["n"] += 1
        return "ran"

    async def go():
        dr._RETRY_AWARE_TOOL_CALLS["sim_gate"] = raising
        t = SimpleNamespace(name="sim_gate")
        # 策略按真实工具名判断；这里用 python_executor 名字走闸门，再映射到 sim 入口
        dr._RETRY_AWARE_TOOL_CALLS[tool_name] = raising
        ap.set_broker(broker)
        cfg = SimpleNamespace(tool_retry_max_attempts=1, tool_retry_base_delay_seconds=0.0,
                              human_approval_enabled=enabled, human_approval_timeout_seconds=1)
        tool = SimpleNamespace(name=tool_name)
        task = asyncio.create_task(dr.execute_tool_safely(tool, args, config=None, configurable=cfg))
        if decision is not None:
            await asyncio.sleep(0.05)
            pend = broker.list_pending()
            assert len(pend) == 1
            broker.decide(pend[0]["id"], decision)
        try:
            return await task
        finally:
            dr._RETRY_AWARE_TOOL_CALLS.pop("sim_gate", None)
            if tool_name != "python_executor":
                dr._RETRY_AWARE_TOOL_CALLS.pop(tool_name, None)
            ap.set_broker(None)
    out = run(go())
    return out, spy["n"]


def test_execute_tool_safely_default_off_runs_without_gate():
    out, n = _execute("python_executor", {"code": "x"}, enabled=False, broker=None)
    assert out[1] == "success" and n == 1
    print("✅ test_execute_tool_safely_default_off_runs_without_gate 通过")


def test_execute_tool_safely_approved_runs_once_denied_never_runs():
    b = ap.InMemoryApprovalBroker()
    out, n = _execute("python_executor", {"code": "x"}, True, b, decision=True)
    assert out[1] == "success" and n == 1
    b = ap.InMemoryApprovalBroker()
    out, n = _execute("python_executor", {"code": "x"}, True, b, decision=False)
    assert n == 0 and out[1] == "error" and out[2] == "tool_input_error" and "denied" in out[0]
    print("✅ test_execute_tool_safely_approved_runs_once_denied_never_runs 通过")


def test_execute_tool_safely_no_broker_fails_closed_and_timeout_not_infra():
    out, n = _execute("python_executor", {"code": "x"}, True, None)
    assert n == 0 and "no_broker" in out[0] and out[2] == "tool_input_error"
    out, n = _execute("python_executor", {"code": "x"}, True, ap.InMemoryApprovalBroker())  # 1s 后超时
    assert n == 0 and "timeout" in out[0] and out[2] == "tool_input_error"
    print("✅ test_execute_tool_safely_no_broker_fails_closed_and_timeout_not_infra 通过")


def test_execute_tool_safely_low_risk_tool_skips_gate_even_when_enabled():
    out, n = _execute("internal_doc_search", {"query": "x"}, True, None)
    assert out[1] == "success" and n == 1
    print("✅ test_execute_tool_safely_low_risk_tool_skips_gate_even_when_enabled 通过")


def test_service_approval_endpoints():
    async def flow():
        r = fr.FakeRedis()
        broker = ap.RedisApprovalBroker(r, poll_interval=0.01)

        async def runner(q):
            return "ok"

        app = create_app(r, runner, Settings(), approval_broker=broker)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            t = asyncio.create_task(broker.request("python_executor: {}", 2.0))
            await asyncio.sleep(0.05)
            pend = (await c.get("/approvals")).json()["pending"]
            assert len(pend) == 1
            rid = pend[0]["id"]
            assert (await c.post(f"/approvals/{rid}", json={"approve": True})).json()["decision"] == "approved"
            assert await t == ap.APPROVED
            assert (await c.post(f"/approvals/{rid}", json={"approve": False})).status_code == 404
            assert (await c.get("/approvals")).json()["pending"] == []
    run(flow())
    print("✅ test_service_approval_endpoints 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
