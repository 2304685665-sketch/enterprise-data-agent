"""T2 容错层（execute_tool_safely 重试 + researcher_tools 的未知工具名保护/
熔断/外层超时）的自动化测试。

分两类，不要混为一谈：
- 大多数场景用 mock（patch _RETRY_AWARE_TOOL_CALLS 里的入口），不消耗真实
  API，也不依赖 Docker 在跑，验证的是 execute_tool_safely / researcher_tools
  本身的控制流逻辑是否正确。
- test_outer_timeout_larger_than_inner_does_not_leak_container 是唯一一个
  真实跑 Docker 容器的测试（不是 mock），因为这条要验证的是"外层超时 >
  内部超时"这个设计选择在真实容器清理机制下确实安全——这个保证没办法用
  mock 可信地验证，必须让真实的 docker kill + 轮询确认机制跑一遍。和现有
  18 个 python_executor 测试用的是同一套安全模型（一次性、--rm 自动清理）。

运行方式：和项目里其他测试一样是独立脚本（`asyncio.run(main())`），不依赖
pytest-asyncio 的 auto 模式（本仓库的 pytest 没有配置 asyncio_mode，装饰器
风格的 @pytest.mark.asyncio 在 strict 模式下跑不起来，这点在诊断阶段已经
确认过，不是本次新发现的 bug）。
"""

import asyncio
import subprocess as sp
import time
from unittest.mock import patch

from langchain_core.messages import AIMessage

from open_deep_research.deep_researcher import (
    execute_tool_safely,
    researcher_tools,
    _RETRY_AWARE_TOOL_CALLS,
    _looks_like_tool_error,
)
from open_deep_research.configuration import Configuration
from open_deep_research.tool_reliability import TransientToolError
from open_deep_research.internal_doc_search import internal_doc_search_tool_call


class _FakeInvokableTool:
    """给没有注册"抛异常版"入口的工具用（比如 sql_executor）——
    execute_tool_safely 对这类工具走的是 tool.ainvoke(args, config)。"""
    def __init__(self, name, ainvoke_fn):
        self.name = name
        self.ainvoke = ainvoke_fn


async def _fake_get_all_tools_returning(tools, config):
    return tools


class _FakeTool:
    """execute_tool_safely 只读 tool.name，不需要真的是 LangChain BaseTool。"""
    def __init__(self, name):
        self.name = name


def _fast_config(max_attempts=2, base_delay=0.01, circuit_threshold=3, outer_timeout=5.0):
    return Configuration(
        tool_retry_max_attempts=max_attempts,
        tool_retry_base_delay_seconds=base_delay,
        tool_circuit_breaker_threshold=circuit_threshold,
        tool_call_timeout_seconds=outer_timeout,
    )


def _make_state(tool_name, args, tool_failure_counts=None):
    msg = AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "1"}])
    return {
        "researcher_messages": [msg],
        "tool_call_iterations": 0,
        "research_topic": "test",
        "compressed_research": "",
        "raw_notes": [],
        "tool_failure_counts": tool_failure_counts or {},
    }


# ---------------------------------------------------------------------------
# 场景1：工具首次调用成功，不发生重试
# ---------------------------------------------------------------------------

async def test_first_call_succeeds_no_retry():
    call_count = 0

    async def fake_call(code):
        nonlocal call_count
        call_count += 1
        return "ok"

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_call}):
        content, status, failure_kind, attempts = await execute_tool_safely(
            _FakeTool("python_executor"), {"code": "..."}, {}, _fast_config()
        )
    assert content == "ok"
    assert status == "success"
    assert failure_kind == "none"
    assert call_count == 1
    print("✅ test_first_call_succeeds_no_retry 通过")


# ---------------------------------------------------------------------------
# 场景2：临时错误后恢复，重试后成功
# ---------------------------------------------------------------------------

async def test_transient_error_recovers_after_retry():
    call_count = 0

    async def fake_call(code):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise TransientToolError("Python Error: failed to start the sandbox container: boom")
        return "ok after retry"

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_call}):
        content, status, failure_kind, attempts = await execute_tool_safely(
            _FakeTool("python_executor"), {"code": "..."}, {}, _fast_config(max_attempts=2)
        )
    assert content == "ok after retry"
    assert status == "success"
    assert failure_kind == "none"
    assert call_count == 2
    print(f"✅ test_transient_error_recovers_after_retry 通过（第 {call_count} 次调用成功）")


# ---------------------------------------------------------------------------
# 场景3+6+8：持续失败达到上限后停止；失败状态正确传递；没有无限重试
# ---------------------------------------------------------------------------

async def test_persistent_transient_failure_stops_after_max_retries():
    call_count = 0

    async def fake_call(code):
        nonlocal call_count
        call_count += 1
        raise TransientToolError(f"Python Error: attempt {call_count} failed")

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_call}):
        content, status, failure_kind, attempts = await execute_tool_safely(
            _FakeTool("python_executor"), {"code": "..."}, {}, _fast_config(max_attempts=2)
        )
    # max_attempts=2：首次尝试 + 最多2次重试 = 最多3次调用，不是无限重试
    assert call_count == 3
    assert "attempt 3 failed" in content
    assert status == "error"
    assert failure_kind == "infrastructure_error"
    print(f"✅ test_persistent_transient_failure_stops_after_max_retries 通过"
          f"（固定在 {call_count} 次后停止，失败状态原样传递，归为 {failure_kind}：{content[:60]}...）")


# ---------------------------------------------------------------------------
# 场景4：确定性错误不发生不必要的重试
# ---------------------------------------------------------------------------

async def test_permanent_error_does_not_retry():
    call_count = 0

    async def fake_call(code):
        nonlocal call_count
        call_count += 1
        raise ValueError("Python Error: Traceback ... SyntaxError: invalid syntax")

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_call}):
        content, status, failure_kind, attempts = await execute_tool_safely(
            _FakeTool("python_executor"), {"code": "..."}, {}, _fast_config(max_attempts=2)
        )
    assert call_count == 1  # 未被分类为 Transient，不重试
    assert "Error executing tool:" in content
    assert status == "error"
    # 这是一个"未被分类成瞬时性错误的其他异常"（ValueError 不在重试分类里），
    # 走的是 execute_tool_safely 的 generic except Exception 分支，异常真的
    # 逃出来了，归为 infrastructure_error——这和 sql_executor/python_executor
    # 自己处理掉返回字符串的"代码报错"是两回事，不要混淆。
    assert failure_kind == "infrastructure_error"
    print(f"✅ test_permanent_error_does_not_retry 通过（只调用1次，不重试，归为 {failure_kind}）")


# ---------------------------------------------------------------------------
# 场景5：超时被正确识别和处理（外层 wait_for）
# ---------------------------------------------------------------------------

async def test_outer_timeout_is_recognized():
    async def slow_call(code):
        await asyncio.sleep(10)
        return "too slow"

    state = _make_state("python_executor", {"code": "..."})
    config = {"configurable": {"tool_call_timeout_seconds": 0.2, "tool_retry_max_attempts": 0}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": slow_call}):
        start = time.monotonic()
        result = await researcher_tools(state, config)
        elapsed = time.monotonic() - start

    assert elapsed < 2, f"应该被外层超时很快截断，实际耗时 {elapsed:.2f}s"
    tm = result.update["researcher_messages"][0]
    assert tm.status == "error"
    assert "did not complete within" in tm.content
    print(f"✅ test_outer_timeout_is_recognized 通过（{elapsed:.2f}s 内被外层超时截断）")


# ---------------------------------------------------------------------------
# 场景7：工具返回空结果时，不将其误判为执行异常
# ---------------------------------------------------------------------------

async def test_empty_result_not_misjudged_as_error():
    async def fake_call(query, n_results=3):
        return ""  # 合法的"无结果"返回，不是异常

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": fake_call}):
        content, status, failure_kind, attempts = await execute_tool_safely(
            _FakeTool("internal_doc_search"), {"query": "x"}, {}, _fast_config()
        )
    assert content == ""
    assert status == "success"
    assert failure_kind == "none"
    print("✅ test_empty_result_not_misjudged_as_error 通过（空字符串不被判成失败）")


# ---------------------------------------------------------------------------
# 场景9：并发调用不会导致不同任务的重试/熔断状态相互污染
# ---------------------------------------------------------------------------

async def test_concurrent_runs_do_not_share_circuit_breaker_state():
    async def failing_call(code):
        raise ValueError("deterministic failure")

    config = {"configurable": {"tool_circuit_breaker_threshold": 3, "tool_retry_max_attempts": 0}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": failing_call}):
        # run_a 的 state 里已经记录了3次连续失败（达到熔断阈值），
        # run_b 是全新的独立 state，从0开始——两者并发跑，验证互不影响。
        state_a = _make_state("python_executor", {"code": "x"}, {"python_executor": 3})
        state_b = _make_state("python_executor", {"code": "x"}, {"python_executor": 0})
        result_a, result_b = await asyncio.gather(
            researcher_tools(state_a, config),
            researcher_tools(state_b, config),
        )

    tm_a = result_a.update["researcher_messages"][0]
    tm_b = result_b.update["researcher_messages"][0]
    assert "temporarily marked unavailable" in tm_a.content
    assert "temporarily marked unavailable" not in tm_b.content
    assert "deterministic failure" in tm_b.content
    print("✅ test_concurrent_runs_do_not_share_circuit_breaker_state 通过"
          "（run_a 被熔断，run_b 不受影响，独立判定）")


# ---------------------------------------------------------------------------
# 场景10：不会因重复执行有副作用的操作而引入额外风险
# （结构性保证：sql_executor 根本不在重试注册表里，不依赖任何运行时判断）
# ---------------------------------------------------------------------------

def test_sql_executor_is_never_retry_registered():
    assert "sql_executor" not in _RETRY_AWARE_TOOL_CALLS
    print("✅ test_sql_executor_is_never_retry_registered 通过"
          "（sql_executor 没有'抛异常版'入口，结构上不可能被重试）")


# ---------------------------------------------------------------------------
# 要求 a：未知工具名不崩溃，且同一批次里其他正常调用仍正确执行
# ---------------------------------------------------------------------------

async def test_unknown_tool_name_does_not_crash_other_calls_in_same_batch():
    async def fake_python_call(code):
        return "real result"

    msg = AIMessage(content="", tool_calls=[
        {"name": "nonexistent_hallucinated_tool", "args": {}, "id": "1"},
        {"name": "python_executor", "args": {"code": "print(1)"}, "id": "2"},
    ])
    state = {
        "researcher_messages": [msg], "tool_call_iterations": 0,
        "research_topic": "t", "compressed_research": "", "raw_notes": [],
        "tool_failure_counts": {},
    }
    config = {"configurable": {}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_python_call}):
        result = await researcher_tools(state, config)  # 不应该抛异常

    tool_outputs = result.update["researcher_messages"]
    assert len(tool_outputs) == 2
    unknown_msg = next(m for m in tool_outputs if m.tool_call_id == "1")
    real_msg = next(m for m in tool_outputs if m.tool_call_id == "2")
    assert unknown_msg.status == "error"
    assert "unknown tool" in unknown_msg.content
    assert "python_executor" in unknown_msg.content  # 可用工具名列表里包含真实工具
    assert real_msg.status == "success"
    assert real_msg.content == "real result"
    print("✅ test_unknown_tool_name_does_not_crash_other_calls_in_same_batch 通过"
          "（幻觉工具名得到清晰错误，同批次的真实工具调用不受影响）")


# ---------------------------------------------------------------------------
# 要求 e：熔断在连续失败达到阈值后生效，成功后计数清零
# ---------------------------------------------------------------------------

async def test_circuit_breaker_trips_after_consecutive_failures_then_blocks():
    call_count = 0

    async def always_fails(code):
        nonlocal call_count
        call_count += 1
        raise ValueError("deterministic failure")

    config = {"configurable": {"tool_circuit_breaker_threshold": 2, "tool_retry_max_attempts": 0}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": always_fails}):
        state1 = _make_state("python_executor", {"code": "x"}, {})
        result1 = await researcher_tools(state1, config)
        counts1 = result1.update["tool_failure_counts"]
        assert counts1["python_executor"] == 1 and call_count == 1

        state2 = _make_state("python_executor", {"code": "x"}, counts1)
        result2 = await researcher_tools(state2, config)
        counts2 = result2.update["tool_failure_counts"]
        assert counts2["python_executor"] == 2 and call_count == 2

        # 第3次：达到阈值2，应该被熔断，真正的工具调用不应该再发生
        state3 = _make_state("python_executor", {"code": "x"}, counts2)
        result3 = await researcher_tools(state3, config)
        assert call_count == 2, "熔断生效后不应该再真的调用工具"
        tm3 = result3.update["researcher_messages"][0]
        assert "temporarily marked unavailable" in tm3.content

    print(f"✅ test_circuit_breaker_trips_after_consecutive_failures_then_blocks 通过"
          f"（连续2次失败后熔断，第3次直接拦截，工具实际只被调用了 {call_count} 次）")


async def test_circuit_breaker_resets_on_success():
    async def succeeds(code):
        return "ok"

    config = {"configurable": {"tool_circuit_breaker_threshold": 2, "tool_retry_max_attempts": 0}}
    state = _make_state("python_executor", {"code": "x"}, {"python_executor": 1})

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": succeeds}):
        result = await researcher_tools(state, config)

    assert result.update["tool_failure_counts"]["python_executor"] == 0
    print("✅ test_circuit_breaker_resets_on_success 通过（一次成功后连续失败计数清零）")


# ---------------------------------------------------------------------------
# 2026-10 修复后新增：熔断必须基于 failure_kind，不能基于 status
# ---------------------------------------------------------------------------

async def test_sql_style_input_errors_do_not_trip_circuit_breaker():
    """连续5条"SQL Error: ..."不应该触发熔断——这是模型探索表结构时自我
    纠错的正常过程（sql_executor 的设计本意就是把原始报错给模型自己看），
    不代表 sql_executor 这个工具本身坏了。sql_executor 没有在
    _RETRY_AWARE_TOOL_CALLS 里注册，所以 execute_tool_safely 对它走的是
    tool.ainvoke(args, config) 这条路径——用一个假的、同名的可 ainvoke 对象
    替换 get_all_tools 的返回值来模拟。
    """
    call_count = 0

    async def fake_sql_ainvoke(args, config):
        nonlocal call_count
        call_count += 1
        return f"SQL Error: no such column: wrong_col_{call_count}"

    fake_tool = _FakeInvokableTool("sql_executor", fake_sql_ainvoke)
    config = {"configurable": {"tool_circuit_breaker_threshold": 3}}

    with patch(
        "open_deep_research.deep_researcher.get_all_tools",
        new=lambda cfg: _fake_get_all_tools_returning([fake_tool], cfg),
    ):
        failure_counts = {}
        for _ in range(5):
            state = _make_state("sql_executor", {"query": "SELECT wrong"}, failure_counts)
            result = await researcher_tools(state, config)
            failure_counts = result.update["tool_failure_counts"]
            tm = result.update["researcher_messages"][0]
            assert tm.status == "error"
            assert "SQL Error" in tm.content
            assert "temporarily marked unavailable" not in tm.content

    assert call_count == 5, "sql_executor 应该每次都被真的调用，不应该被熔断拦截"
    assert failure_counts.get("sql_executor", 0) == 0, (
        f"tool_input_error 不应该让熔断计数增长，实际是 "
        f"{failure_counts.get('sql_executor', 0)}"
    )
    print(f"✅ test_sql_style_input_errors_do_not_trip_circuit_breaker 通过"
          f"（连续5次SQL错误，sql_executor 全部被真实调用 {call_count} 次，"
          f"熔断计数始终是 {failure_counts.get('sql_executor', 0)}）")


async def test_three_consecutive_infrastructure_failures_trip_breaker():
    """连续3次基础设施故障（异常真的逃出来）后熔断，阈值用生产默认值3。"""
    call_count = 0

    async def always_fails(code):
        nonlocal call_count
        call_count += 1
        raise ValueError("infrastructure-level failure")

    config = {"configurable": {"tool_circuit_breaker_threshold": 3, "tool_retry_max_attempts": 0}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": always_fails}):
        counts = {}
        for i in range(3):
            state = _make_state("python_executor", {"code": "x"}, counts)
            result = await researcher_tools(state, config)
            counts = result.update["tool_failure_counts"]
        assert counts["python_executor"] == 3 and call_count == 3

        # 第4次：应该被熔断，真正的工具调用不应该再发生
        state = _make_state("python_executor", {"code": "x"}, counts)
        result = await researcher_tools(state, config)
        assert call_count == 3, "熔断生效后不应该再真的调用工具"
        tm = result.update["researcher_messages"][0]
        assert "temporarily marked unavailable" in tm.content

    print(f"✅ test_three_consecutive_infrastructure_failures_trip_breaker 通过"
          f"（连续3次基础设施故障后熔断，第4次直接拦截，工具实际只被调用了 {call_count} 次）")


async def test_success_in_between_resets_count_and_prevents_trip():
    """2次基础设施故障 -> 1次成功（清零）-> 2次基础设施故障：不应该触发
    阈值为3的熔断，因为中间的成功已经把计数清零过一次。"""
    call_log = []

    async def flaky(code):
        call_log.append(1)
        if len(call_log) == 3:  # 第3次调用成功，其余都失败
            return "ok"
        raise ValueError("infrastructure-level failure")

    config = {"configurable": {"tool_circuit_breaker_threshold": 3, "tool_retry_max_attempts": 0}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": flaky}):
        counts = {}
        for _ in range(2):  # 第1、2次：失败，计数到2
            state = _make_state("python_executor", {"code": "x"}, counts)
            result = await researcher_tools(state, config)
            counts = result.update["tool_failure_counts"]
        assert counts["python_executor"] == 2

        state = _make_state("python_executor", {"code": "x"}, counts)  # 第3次：成功，清零
        result = await researcher_tools(state, config)
        counts = result.update["tool_failure_counts"]
        assert counts["python_executor"] == 0

        last_tm = None
        for _ in range(2):  # 第4、5次：失败，计数到2（没有触及阈值3）
            state = _make_state("python_executor", {"code": "x"}, counts)
            result = await researcher_tools(state, config)
            counts = result.update["tool_failure_counts"]
            last_tm = result.update["researcher_messages"][0]

    assert counts["python_executor"] == 2
    assert "temporarily marked unavailable" not in last_tm.content
    print("✅ test_success_in_between_resets_count_and_prevents_trip 通过"
          "（中间一次成功清零计数，后续2次失败不足以触发阈值为3的熔断）")


async def test_tool_input_error_does_not_change_infra_failure_count():
    """2次基础设施故障后，插入一次 tool_input_error（代码报错，正常返回
    字符串不抛异常）：计数必须原样保持在2，既不清零也不增加。"""
    responses = iter([
        "raise", "raise",
        "Python Error: Traceback (most recent call last):\nSyntaxError: invalid syntax",
    ])

    async def mixed(code):
        r = next(responses)
        if r == "raise":
            raise ValueError("infrastructure-level failure")
        return r

    config = {"configurable": {"tool_circuit_breaker_threshold": 5, "tool_retry_max_attempts": 0}}

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": mixed}):
        counts = {}
        for _ in range(2):
            state = _make_state("python_executor", {"code": "x"}, counts)
            result = await researcher_tools(state, config)
            counts = result.update["tool_failure_counts"]
        assert counts["python_executor"] == 2

        state = _make_state("python_executor", {"code": "x"}, counts)
        result = await researcher_tools(state, config)
        counts = result.update["tool_failure_counts"]
        tm = result.update["researcher_messages"][0]

    assert counts["python_executor"] == 2, (
        f"tool_input_error 不应该改变熔断计数，实际变成了 {counts['python_executor']}"
    )
    assert tm.status == "error"
    print("✅ test_tool_input_error_does_not_change_infra_failure_count 通过"
          f"（代码报错后计数保持在 {counts['python_executor']}，不清零也不增加）")


# ---------------------------------------------------------------------------
# 要求2：验证 Chroma/Embedding 层的 openai.APIConnectionError 真的被
# internal_doc_search_tool_call 分类成 TransientToolError
# ---------------------------------------------------------------------------

async def test_internal_doc_search_network_error_classified_as_transient():
    """用真实的 chromadb.EphemeralClient（纯内存，不落盘）+ 真实的
    OpenAIEmbeddingFunction（假 api_key，从不会真的用到——因为在它内部调用
    openai 客户端之前就已经被 patch 拦截）+ patch openai 客户端底层的
    Embeddings.create（而不是 patch collection.query 这种更高层、会跳过
    Chroma 自己代码路径的做法），验证异常类型穿过 Chroma 的调用链之后
    有没有被包装成别的类型。全程不产生真实网络请求——patch 在任何真实
    HTTP 调用发生之前就生效，已经单独用一个独立脚本验证过
    mock_create.call_count == 1 且没有真实网络活动。
    """
    import httpx
    import openai
    import chromadb
    from chromadb.utils import embedding_functions

    fake_request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    conn_error = openai.APIConnectionError(request=fake_request)

    # _run_search() 内部用的是 client.get_collection(...)（要求集合已存在，
    # 不会自动创建），所以要在 patch Embeddings.create 之前，先用同一个
    # embedding_function 对象创建好一个空集合（不加文档，创建空集合这一步
    # 本身不会触发任何 embedding 调用，已经单独验证过）。还要用同一个
    # EphemeralClient 实例贯穿整个测试——每次 new 一个 EphemeralClient() 会
    # 是一个全新的空内存库，看不到之前创建的集合。
    openai_ef = embedding_functions.OpenAIEmbeddingFunction(
        api_key="sk-fake-test-key-no-real-network-calls",
        model_name="text-embedding-3-small",
    )
    shared_client = chromadb.EphemeralClient()
    shared_client.create_collection(name="company_knowledge_base", embedding_function=openai_ef)

    raised = None
    with patch("openai.resources.embeddings.Embeddings.create", side_effect=conn_error) as mock_create:
        with patch(
            "open_deep_research.internal_doc_search.chromadb.PersistentClient",
            return_value=shared_client,
        ):
            # internal_doc_search.py 的 _run_search() 内部会自己再构造一个
            # OpenAIEmbeddingFunction（从环境变量读 api_key），和上面创建集合
            # 用的不是同一个对象，但类名/model_name 相同，chromadb 按配置名
            # （"openai"）而不是对象身份比对，不会冲突。
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-fake-test-key-no-real-network-calls"}):
                try:
                    await internal_doc_search_tool_call("test query")
                except Exception as e:
                    raised = e

    if not isinstance(raised, TransientToolError):
        print(
            f"⚠️ 分类失败！openai.APIConnectionError 经过真实的 Chroma "
            f"EphemeralClient + OpenAIEmbeddingFunction 调用链之后，"
            f"internal_doc_search_tool_call 实际抛出的是: "
            f"{type(raised).__name__ if raised else '没有抛出任何异常'}"
            f"{': ' + str(raised) if raised else ''}"
        )
    assert mock_create.called, "patched Embeddings.create 没有被调用到——说明测试没有真的走到网络调用这一步"
    assert isinstance(raised, TransientToolError), (
        f"预期 TransientToolError，实际是 {type(raised).__name__ if raised else None}"
    )
    print(f"✅ test_internal_doc_search_network_error_classified_as_transient 通过"
          f"（真实 Chroma 调用链下 openai.APIConnectionError 未被包装掩盖，"
          f"正确分类为 TransientToolError；patched Embeddings.create 调用次数="
          f"{mock_create.call_count}，零真实网络请求）")


async def test_internal_doc_search_timeout_is_infrastructure_error_and_not_retried():
    """internal_doc_search 超时之前被归为 tool_input_error（错的，已修复）。
    现在应该是：计入熔断的 infrastructure_error，但不重试（放弃等待的后台
    线程仍在运行，重试会让这类线程堆积，见 NonRetryableInfrastructureError
    的文档字符串）。"""
    call_count = 0

    async def always_times_out(query, n_results=3):
        nonlocal call_count
        call_count += 1
        from open_deep_research.tool_reliability import NonRetryableInfrastructureError
        raise NonRetryableInfrastructureError(
            "Document search error: query did not complete within 15s and was abandoned."
        )

    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": always_times_out}):
        content, status, failure_kind, attempts = await execute_tool_safely(
            _FakeTool("internal_doc_search"), {"query": "x"}, {}, _fast_config(max_attempts=2)
        )

    assert call_count == 1, f"不应该重试，实际调用了 {call_count} 次"
    assert status == "error"
    assert failure_kind == "infrastructure_error"
    assert "did not complete within" in content
    print(f"✅ test_internal_doc_search_timeout_is_infrastructure_error_and_not_retried 通过"
          f"（只调用1次（不重试），归为 {failure_kind}）")


# ---------------------------------------------------------------------------
# 要求 d：外层超时 > 内部超时时不残留容器（真实 Docker，不是 mock）
# ---------------------------------------------------------------------------

async def test_outer_timeout_larger_than_inner_does_not_leak_container():
    """用生产默认的 tool_call_timeout_seconds（30s）跑一段会触发 python_executor
    自身15s超时的代码，验证：(a) 是 python_executor 自己的超时先触发、不是被
    外层30s安全网打断；(b) 容器被干净地清理，没有残留。
    """
    state = _make_state("python_executor", {"code": "import time\ntime.sleep(60)"})
    config = {"configurable": {}}  # 用全部默认值，包括 tool_call_timeout_seconds=30.0

    start = time.monotonic()
    result = await researcher_tools(state, config)
    elapsed = time.monotonic() - start

    tm = result.update["researcher_messages"][0]
    assert tm.status == "error"
    assert ("timeout" in tm.content.lower()), f"未命中预期的超时错误: {tm.content[:200]}"
    assert "outer safety timeout" not in tm.content, (
        "不应该是被外层30s安全网打断——应该是python_executor自己的15s超时先触发"
    )
    assert elapsed < 25, f"应该在python_executor自己的超时附近完成，实际耗时 {elapsed:.1f}s"

    ps_result = sp.run(
        ["docker", "ps", "-a", "--filter", "name=odr-python-executor-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    leaked = [n for n in ps_result.stdout.strip().split("\n") if n]
    assert leaked == [], f"发现残留容器: {leaked}"
    print(f"✅ test_outer_timeout_larger_than_inner_does_not_leak_container 通过"
          f"（{elapsed:.1f}s 完成，由内层超时触发而非外层30s安全网，docker ps 确认无残留容器）")


async def main():
    await test_first_call_succeeds_no_retry()
    await test_transient_error_recovers_after_retry()
    await test_persistent_transient_failure_stops_after_max_retries()
    await test_permanent_error_does_not_retry()
    await test_outer_timeout_is_recognized()
    await test_empty_result_not_misjudged_as_error()
    await test_concurrent_runs_do_not_share_circuit_breaker_state()
    test_sql_executor_is_never_retry_registered()
    await test_unknown_tool_name_does_not_crash_other_calls_in_same_batch()
    await test_circuit_breaker_trips_after_consecutive_failures_then_blocks()
    await test_circuit_breaker_resets_on_success()
    await test_sql_style_input_errors_do_not_trip_circuit_breaker()
    await test_three_consecutive_infrastructure_failures_trip_breaker()
    await test_success_in_between_resets_count_and_prevents_trip()
    await test_tool_input_error_does_not_change_infra_failure_count()
    await test_internal_doc_search_network_error_classified_as_transient()
    await test_internal_doc_search_timeout_is_infrastructure_error_and_not_retried()
    print("\n--- 以下这一条真实跑 Docker，预计耗时约15-20秒 ---")
    await test_outer_timeout_larger_than_inner_does_not_leak_container()
    print("\n全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
