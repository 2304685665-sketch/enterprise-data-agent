"""T3（数据源降级披露 + 引用核验）的自动化测试。

延续 test_tool_reliability.py 的风格：独立脚本，每个测试是一个独立函数，
main() 依次跑，不依赖 pytest-asyncio（本仓库 pytest 没配 asyncio_mode）。

一个强制要求（用户审批设计时明确要求，不能自行降级为更简单的做法）：
并行researcher事件合并这条测试必须真的跑通编译好的LangGraph图
（researcher_subgraph + supervisor_subgraph），不能只测reducer函数本身——
见 test_full_graph_parallel_researchers_degradation_merge_and_disclosure。

为此需要一个"按输入消息内容决定输出"的假模型（_ContentAwareFakeChatModel），
刻意不用LangChain自带的GenericFakeChatModel——那个是固定顺序的迭代器，多个
并行researcher共享同一个模型实例时，并发调用谁先谁后不确定，会把预先排好的
回复序列发给错的调用者。改成按每次调用自己收到的messages结构判断该返回
什么，天然并发安全（不依赖任何共享可变状态，纯函数式决策）。
"""

import asyncio
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from open_deep_research import citation_verification as cv
from open_deep_research import deep_researcher as dr
from open_deep_research import internal_doc_search as ids_mod
from open_deep_research.prompts import (
    compress_research_simple_human_message,
    final_report_generation_prompt,
    research_system_prompt,
)

# ===========================================================================
# 第一部分：researcher_tools 产生 degradation_events / fetched_sources 的
# 各种场景（直接调用真实的 researcher_tools 函数，mock 掉 _RETRY_AWARE_TOOL_CALLS
# 入口——和 test_tool_reliability.py 已有测试的做法一致）
# ===========================================================================


def _make_state(tool_name, args, tool_failure_counts=None, research_topic="test-topic"):
    msg = AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "1"}])
    return {
        "researcher_messages": [msg],
        "tool_call_iterations": 0,
        "research_topic": research_topic,
        "compressed_research": "",
        "raw_notes": [],
        "tool_failure_counts": tool_failure_counts or {},
    }


async def test_circuit_breaker_open_records_degradation_event_with_zero_attempts():
    state = _make_state(
        "internal_doc_search", {"query": "x"},
        tool_failure_counts={"internal_doc_search": 3},
        research_topic="熔断测试主题",
    )
    config = {"configurable": {"tool_circuit_breaker_threshold": 3}}
    result = await dr.researcher_tools(state, config)
    events = result.update["degradation_events"]
    assert len(events) == 1, f"应该恰好产生1条事件，实际: {events}"
    e = events[0]
    assert e["source"] == "internal_doc_search"
    assert e["reason"] == "circuit_breaker_open"
    assert e["failure_kind"] == "infrastructure_error"
    assert e["attempts"] == 0, "熔断拦截时工具根本没被真的调用，attempts必须是0"
    assert e["research_topic"] == "熔断测试主题"
    assert e["type"] == "source_unavailable"
    print("✅ test_circuit_breaker_open_records_degradation_event_with_zero_attempts 通过")


async def test_retries_exhausted_records_degradation_event_with_correct_attempts():
    from open_deep_research.tool_reliability import TransientToolError
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    async def always_fails(code):
        raise TransientToolError("Python Error: boom")

    state = _make_state("python_executor", {"code": "..."}, research_topic="重试耗尽主题")
    config = {"configurable": {"tool_retry_max_attempts": 2, "tool_retry_base_delay_seconds": 0.01}}
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": always_fails}):
        result = await dr.researcher_tools(state, config)
    events = result.update["degradation_events"]
    assert len(events) == 1
    e = events[0]
    assert e["reason"] == "retries_exhausted"
    assert e["attempts"] == 3, "max_attempts=2意味着首次尝试+最多2次重试=3次"
    assert e["research_topic"] == "重试耗尽主题"
    print("✅ test_retries_exhausted_records_degradation_event_with_correct_attempts 通过")


async def test_outer_timeout_records_degradation_event_with_attempts_none():
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    async def slow_call(code):
        await asyncio.sleep(10)
        return "too slow"

    state = _make_state("python_executor", {"code": "..."}, research_topic="外层超时主题")
    config = {"configurable": {"tool_call_timeout_seconds": 0.2, "tool_retry_max_attempts": 0}}
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": slow_call}):
        result = await dr.researcher_tools(state, config)
    events = result.update["degradation_events"]
    assert len(events) == 1
    e = events[0]
    assert e["reason"] == "outer_timeout", (
        "外层安全超时和execute_tool_safely内部真的重试耗尽是两种不同的确定性——"
        "前者不知道工具是不是终究会成功，不能和retries_exhausted混用同一个reason"
    )
    assert e["attempts"] is None, "外层安全超时打断时不知道内部跑到第几次，必须如实标None，不能瞎猜"
    assert e["research_topic"] == "外层超时主题"
    print("✅ test_outer_timeout_records_degradation_event_with_attempts_none 通过")


async def test_tool_input_error_does_not_record_degradation_event():
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    async def input_error(code):
        return "Python Error: Traceback ... SyntaxError: invalid syntax"

    state = _make_state("python_executor", {"code": "bad"})
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": input_error}):
        result = await dr.researcher_tools(state, {"configurable": {}})
    assert result.update["degradation_events"] == [], (
        "模型自我纠错的代码错误是正常流程的一部分，不应该被当成数据源不可用"
    )
    print("✅ test_tool_input_error_does_not_record_degradation_event 通过")


async def test_successful_call_does_not_record_degradation_event():
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    async def ok_call(code):
        return "all good"

    state = _make_state("python_executor", {"code": "ok"})
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
        result = await dr.researcher_tools(state, {"configurable": {}})
    assert result.update["degradation_events"] == []
    print("✅ test_successful_call_does_not_record_degradation_event 通过")


async def test_unknown_tool_name_does_not_record_degradation_event():
    state = _make_state("this_tool_does_not_exist", {"foo": "bar"})
    result = await dr.researcher_tools(state, {"configurable": {}})
    assert result.update["degradation_events"] == [], (
        "幻觉工具名是'根本没有这个源'，不是'这个源不可用'，语义不同，刻意不记"
    )
    print("✅ test_unknown_tool_name_does_not_record_degradation_event 通过")


async def test_fetched_sources_extracted_only_on_success_status():
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    tavily_style_success = (
        "\n\n--- SOURCE 1: Example Page ---\nURL: https://example.com/a\n\n"
        "SUMMARY:\nsome content\n\n"
        "\n\n--- SOURCE 2: Second Page ---\nURL: https://example.com/b\n\nSUMMARY:\nmore\n\n"
    )

    async def fake_success(query, n_results=3):
        return tavily_style_success

    state = _make_state("internal_doc_search", {"query": "q"}, research_topic="来源提取主题")
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": fake_success}):
        result = await dr.researcher_tools(state, {"configurable": {}})
    sources = result.update["fetched_sources"]
    assert len(sources) == 2, f"应该提取到2个URL，实际: {sources}"
    assert {s["url"] for s in sources} == {"https://example.com/a", "https://example.com/b"}
    assert all(s["source"] == "internal_doc_search" and s["research_topic"] == "来源提取主题" for s in sources)
    print("✅ test_fetched_sources_extracted_only_on_success_status 通过")


async def test_fetched_sources_extraction_against_real_tavily_formatter_output():
    """不自己手写'--- SOURCE X ---\\nURL: ...'这段格式字符串，而是真的调用
    项目里tavily_search这个工具完整的真实代码路径——只mock掉真的会发网络
    请求的tavily_search_async这一层（返回canned数据，且raw_content=None，
    这样连summarize_webpage都不会被真的调用到，不需要额外mock任何模型），
    格式化逻辑（utils.py里"--- SOURCE {i+1} ---\\nURL: {url}..."那几行）
    原样真实执行。这样如果以后格式变了，这条测试会跟着真实代码路径自动
    失败，不依赖我自己手抄的格式字符串是否还和源码保持一致。"""
    from open_deep_research import utils as utils_mod

    async def fake_tavily_search_async(queries, max_results=5, topic="general", include_raw_content=True, config=None):
        return [{
            "query": queries[0],
            "results": [
                {"url": "https://real-tavily-1.com/a", "title": "Result A", "content": "content A", "raw_content": None},
                {"url": "https://real-tavily-2.com/b", "title": "Result B", "content": "content B", "raw_content": None},
            ],
        }]

    config = {"configurable": {}}
    with patch.object(utils_mod, "tavily_search_async", fake_tavily_search_async):
        real_formatted_output = await utils_mod.tavily_search.ainvoke({"queries": ["q"]}, config)
    assert "URL: https://real-tavily-1.com/a" in real_formatted_output, (
        "mock应该只换掉网络那一层，真实格式化逻辑应该原样产出这一行"
    )

    state = _make_state("tavily_search", {"queries": ["q"]}, research_topic="真实tavily格式测试")
    with patch.object(utils_mod, "tavily_search_async", fake_tavily_search_async):
        result = await dr.researcher_tools(state, config)
    sources = result.update["fetched_sources"]
    assert {s["url"] for s in sources} == {"https://real-tavily-1.com/a", "https://real-tavily-2.com/b"}, (
        f"用真实tavily_search产出的文本喂给fetched_sources提取逻辑，应该正确提取出两个URL，实际: {sources}"
    )
    assert all(s["source"] == "tavily_search" and s["research_topic"] == "真实tavily格式测试" for s in sources)
    print("✅ test_fetched_sources_extraction_against_real_tavily_formatter_output 通过"
          "（走的是tavily_search真实格式化代码路径，不是手写字符串）")


async def test_fetched_sources_not_extracted_when_status_is_error_even_if_content_mentions_url():
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    async def fake_error(code):
        return "Python Error: failed while fetching URL: https://example.com/should-not-count"

    state = _make_state("python_executor", {"code": "x"})
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_error}):
        result = await dr.researcher_tools(state, {"configurable": {}})
    assert result.update["fetched_sources"] == [], (
        "status=error的内容不应该被当成真实检索到的来源，即使文本里恰好出现URL字样"
    )
    print("✅ test_fetched_sources_not_extracted_when_status_is_error_even_if_content_mentions_url 通过")


async def test_fetched_sources_extraction_does_not_crash_on_non_string_or_url_less_content():
    """混合两种"没有URL可提取"的内容在同一批tool_calls里：
    - python_executor 正常成功返回，但内容里没有任何URL（最常见的情况）；
    - ResearchComplete 真实工具调用成功时，execute_tool_safely拿到的content
      不是字符串，是ResearchComplete()模型实例本身（这是真实图测试过程中
      真实踩到过的bug，这里补一个不依赖整张图、更快更直接的单测回归）。
    两种情况都应该让提取逻辑全身而退，不崩，且不产出任何fetched_sources。
    """
    from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

    async def plain_success_no_url(code):
        return "42"  # 正常返回，没有任何URL

    msg = AIMessage(content="", tool_calls=[
        {"name": "python_executor", "args": {"code": "print(42)"}, "id": "1"},
        {"name": "ResearchComplete", "args": {}, "id": "2"},
    ])
    state = {
        "researcher_messages": [msg],
        "tool_call_iterations": 0,
        "research_topic": "混合内容测试",
        "compressed_research": "",
        "raw_notes": [],
        "tool_failure_counts": {},
    }
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": plain_success_no_url}):
        result = await dr.researcher_tools(state, {"configurable": {}})  # 不应该抛异常
    assert result.update["fetched_sources"] == [], (
        "非字符串内容（ResearchComplete实例）和无URL的字符串内容都不应该产出任何fetched_sources"
    )
    print("✅ test_fetched_sources_extraction_does_not_crash_on_non_string_or_url_less_content 通过")


# ===========================================================================
# 第二部分：supervisor_tools 合并并行researcher结果（mock researcher_subgraph.ainvoke，
# 测的是supervisor_tools自己的合并逻辑——用extend而不是像raw_notes那样join成字符串）
# ===========================================================================


def _supervisor_state_with_conduct_research(topics):
    tool_calls = [
        {"name": "ConductResearch", "args": {"research_topic": t}, "id": f"call_{i}"}
        for i, t in enumerate(topics)
    ]
    msg = AIMessage(content="", tool_calls=tool_calls)
    return {
        "supervisor_messages": [msg],
        "research_brief": "brief",
        "research_iterations": 1,
        "notes": [],
        "raw_notes": [],
    }


async def test_supervisor_tools_merges_parallel_degradation_events_preserving_topic_via_extend():
    async def fake_ainvoke(payload, config=None):
        topic = payload["research_topic"]
        return {
            "compressed_research": f"summary of {topic}",
            "raw_notes": [f"raw notes for {topic}"],
            "degradation_events": [{
                "type": "source_unavailable", "source": "internal_doc_search",
                "reason": "retries_exhausted", "failure_kind": "infrastructure_error",
                "attempts": 3, "research_topic": topic,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            "fetched_sources": [],
        }

    state = _supervisor_state_with_conduct_research(["Topic 1", "Topic 2"])
    with patch.object(dr.researcher_subgraph, "ainvoke", fake_ainvoke):
        result = await dr.supervisor_tools(state, {"configurable": {}})
    events = result.update["degradation_events"]
    assert len(events) == 2, f"两个并行researcher各产生1条，应该共2条，实际: {events}"
    topics_in_events = {e["research_topic"] for e in events}
    assert topics_in_events == {"Topic 1", "Topic 2"}, (
        "每条事件必须保留自己researcher的research_topic归属，不能像raw_notes那样"
        "join成一个字符串丢失这个信息"
    )
    print("✅ test_supervisor_tools_merges_parallel_degradation_events_preserving_topic_via_extend 通过")


async def test_supervisor_tools_merges_parallel_fetched_sources_via_extend():
    async def fake_ainvoke(payload, config=None):
        topic = payload["research_topic"]
        return {
            "compressed_research": f"summary of {topic}",
            "raw_notes": [],
            "degradation_events": [],
            "fetched_sources": [{"source": "internal_doc_search", "url": f"https://x.com/{topic}", "research_topic": topic}],
        }

    state = _supervisor_state_with_conduct_research(["Topic 1", "Topic 2"])
    with patch.object(dr.researcher_subgraph, "ainvoke", fake_ainvoke):
        result = await dr.supervisor_tools(state, {"configurable": {}})
    sources = result.update["fetched_sources"]
    assert len(sources) == 2
    assert {s["research_topic"] for s in sources} == {"Topic 1", "Topic 2"}
    print("✅ test_supervisor_tools_merges_parallel_fetched_sources_via_extend 通过")


async def test_supervisor_tools_omits_keys_when_no_new_events_present():
    async def fake_ainvoke(payload, config=None):
        return {"compressed_research": "x", "raw_notes": [], "degradation_events": [], "fetched_sources": []}

    state = _supervisor_state_with_conduct_research(["Topic 1"])
    with patch.object(dr.researcher_subgraph, "ainvoke", fake_ainvoke):
        result = await dr.supervisor_tools(state, {"configurable": {}})
    assert "degradation_events" not in result.update, (
        "没有新事件时不应该往update里塞空列表——虽然operator.add对空列表是no-op，"
        "省略key是更干净的写法，这里验证实际实现确实这样做"
    )
    assert "fetched_sources" not in result.update
    print("✅ test_supervisor_tools_omits_keys_when_no_new_events_present 通过")


# ===========================================================================
# 第三部分：最终报告披露小节的纯函数逻辑（去重、幂等）
# ===========================================================================


def test_dedupe_degradation_events_by_source_topic_reason_ignores_timestamp():
    events = [
        {"source": "internal_doc_search", "research_topic": "A", "reason": "retries_exhausted", "timestamp": "t1"},
        {"source": "internal_doc_search", "research_topic": "A", "reason": "retries_exhausted", "timestamp": "t2"},
        {"source": "internal_doc_search", "research_topic": "B", "reason": "retries_exhausted", "timestamp": "t3"},
    ]
    deduped = dr._dedupe_degradation_events(events)
    assert len(deduped) == 2, f"前两条source+topic+reason完全一样（timestamp不参与去重），应该去重成1条: {deduped}"
    assert deduped[0]["timestamp"] == "t1", "去重保留第一次出现的那条"
    print("✅ test_dedupe_degradation_events_by_source_topic_reason_ignores_timestamp 通过")


def test_append_degradation_disclosure_noop_on_empty_events():
    original = "# 报告正文\n没有任何问题。"
    result = dr._append_degradation_disclosure(original, [])
    assert result == original, "events为空时必须原样返回，不改一个字节"
    print("✅ test_append_degradation_disclosure_noop_on_empty_events 通过")


def test_append_degradation_disclosure_noop_if_marker_already_present():
    events = [{"source": "x", "research_topic": "t", "reason": "retries_exhausted"}]
    original = f"# 报告正文\n{dr._DEGRADATION_SECTION_MARKER}\n（已经手工写过这段了）"
    result = dr._append_degradation_disclosure(original, events)
    assert result == original, "标题已存在时不应该重复追加（防止意外被调用多次时重复累加）"
    print("✅ test_append_degradation_disclosure_noop_if_marker_already_present 通过")


def test_append_degradation_disclosure_adds_expected_section():
    events = [{
        "type": "source_unavailable", "source": "internal_doc_search", "reason": "circuit_breaker_open",
        "failure_kind": "infrastructure_error", "attempts": 0, "research_topic": "测试主题",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }]
    original = "# 报告正文"
    result = dr._append_degradation_disclosure(original, events)
    assert result.startswith(original)
    assert dr._DEGRADATION_SECTION_MARKER in result
    assert "internal_doc_search" in result
    assert "测试主题" in result
    assert "2026-01-01T00:00:00" not in result, "timestamp不应该出现在披露小节里"
    print("✅ test_append_degradation_disclosure_adds_expected_section 通过")


def test_outer_timeout_disclosure_wording_does_not_assert_unavailable():
    events = [{
        "type": "source_unavailable", "source": "python_executor", "reason": "outer_timeout",
        "failure_kind": "infrastructure_error", "attempts": None, "research_topic": "超时措辞主题",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }]
    result = dr._append_degradation_disclosure("# 报告正文", events)
    assert "不可用" not in result, "outer_timeout不能下'不可用'这种确定性断言，应该只说调用失败"
    assert "调用失败" in result
    assert "尝试次数：未知" in result
    assert "原因码：outer_timeout" in result
    print("✅ test_outer_timeout_disclosure_wording_does_not_assert_unavailable 通过")


def test_build_degradation_prompt_hint_empty_and_nonempty():
    assert dr._build_degradation_prompt_hint([]) == "", "没有事件时不应该往findings里加任何东西"
    events = [{"source": "internal_doc_search", "reason": "retries_exhausted", "research_topic": "主题X"}]
    hint = dr._build_degradation_prompt_hint(events)
    assert hint != ""
    assert "internal_doc_search" in hint
    assert "主题X" in hint
    print("✅ test_build_degradation_prompt_hint_empty_and_nonempty 通过")


# ===========================================================================
# 第四部分：citation_verification.py
# ===========================================================================


def test_normalize_url_rules():
    assert cv.normalize_url("HTTPS://Example.com:443/Path/") == "https://example.com/Path"
    assert cv.normalize_url("http://example.com:80/x") == "http://example.com/x"
    assert cv.normalize_url("https://example.com/x.") == "https://example.com/x"
    assert cv.normalize_url("https://example.com/x)") == "https://example.com/x"
    # 根路径"/"本身被保留，不裁掉——normalize_url的文档字符串里明确说明了
    # 这一点，避免把"https://example.com"和"https://example.com/"之外的
    # 情况也错误合并；这里按实际设计断言，不是按直觉断言。
    assert cv.normalize_url("https://example.com/") == "https://example.com/"
    assert cv.normalize_url("https://example.com") == "https://example.com"
    print("✅ test_normalize_url_rules 通过")


def test_verify_citations_valid_invalid_missing_categories():
    fetched_sources = [
        {"source": "tavily_search", "url": "https://real1.com/a", "research_topic": "T1"},
        {"source": "tavily_search", "url": "https://real2.com/b/", "research_topic": "T2"},
    ]
    report = (
        "根据调研，参考 https://real1.com/a 可以看到相关数据。\n"
        "另外模型编造了一个来源 https://fabricated.com/not-real 。"
    )
    result = cv.verify_citations(report, fetched_sources)
    assert {v["url"] for v in result["valid"]} == {"https://real1.com/a"}
    assert result["invalid"] == ["https://fabricated.com/not-real"]
    assert {m["url"] for m in result["missing"]} == {"https://real2.com/b"}, (
        "real2.com被真实检索到过，但报告正文完全没提到，应该进missing"
    )
    print("✅ test_verify_citations_valid_invalid_missing_categories 通过")


# ---------------------------------------------------------------------------
# citation_verification 接入 final_report_generation 之后的幂等追加逻辑
# （四种情形：全部可追溯 / 存在不可追溯 / 无来源小节（missing） / fetched_sources为空）
# ---------------------------------------------------------------------------


def test_citation_verification_disclosure_noop_when_all_traceable():
    """场景a：全部引用可追溯 -> 不追加小节。"""
    fetched_sources = [{"source": "tavily_search", "url": "https://real.com/a", "research_topic": "T1"}]
    report = "参考 https://real.com/a 得出结论。"
    result = dr._append_citation_verification_disclosure(report, fetched_sources)
    assert result == report, "全部可追溯时不应该追加任何小节"
    print("✅ test_citation_verification_disclosure_noop_when_all_traceable 通过")


def test_citation_verification_disclosure_noop_when_only_missing_but_verify_citations_still_returns_it():
    """场景b：只有missing（没有invalid）-> 最终报告不追加小节，但
    verify_citations自己的返回结构必须完整保留missing，不受渲染条件变化
    影响——这里直接调用verify_citations验证返回值，再单独验证
    _append_citation_verification_disclosure确实不往报告里追加任何东西，
    两件事分开断言，不要混在一起。
    """
    fetched_sources = [
        {"source": "tavily_search", "url": "https://real.com/a", "research_topic": "T1"},
        {"source": "tavily_search", "url": "https://real.com/b", "research_topic": "T2"},
    ]
    report = "这是一份完全没有引用任何链接的报告正文。"

    # 1) verify_citations本身的计算/返回结构不受本轮改动影响：missing必须
    #    仍然被完整计算出来（两条fetched_sources都没被引用）。
    verification = cv.verify_citations(report, fetched_sources)
    assert verification["invalid"] == []
    assert {m["url"] for m in verification["missing"]} == {"https://real.com/a", "https://real.com/b"}, (
        "verify_citations自己的missing计算/返回结构必须保持不变，不受报告渲染条件的改动影响"
    )

    # 2) 但最终报告的追加逻辑现在只看invalid，只有missing时不应该追加任何东西。
    result = dr._append_citation_verification_disclosure(report, fetched_sources)
    assert result == report, "只有missing、没有invalid时，最终报告不应该追加任何小节"
    print("✅ test_citation_verification_disclosure_noop_when_only_missing_but_verify_citations_still_returns_it 通过")


def test_citation_verification_disclosure_flags_invalid_with_neutral_wording_and_excludes_missing():
    """场景c：存在invalid时才追加小节；措辞改为中性表述；即使同时存在
    missing，报告正文的小节里也不应该列出missing（只展示invalid）。
    """
    fetched_sources = [{"source": "tavily_search", "url": "https://real.com/a", "research_topic": "T1"}]
    # 故意同时构造invalid（fabricated url）和missing（real.com/a被检索到但报告没引用）
    report = "报告只引用了一个模型编造的来源 https://fabricated.com/x ，没有提到其它任何链接。"
    result = dr._append_citation_verification_disclosure(report, fetched_sources)
    assert result.startswith(report)
    assert dr._CITATION_VERIFICATION_SECTION_MARKER in result
    section = result.split(dr._CITATION_VERIFICATION_SECTION_MARKER)[1]
    assert "https://fabricated.com/x" in section
    assert "未能在本次检索记录中匹配到（匹配规则较保守，请自行核实）" in section, "措辞必须是中性表述"
    assert "可能是生成报告时产生的错误" not in result, "不应该再出现这句带确定性归因的旧措辞"
    assert "https://real.com/a" not in section, (
        "即使同时存在missing（real.com/a被检索到但报告没引用），报告正文的小节里也不应该列出missing"
    )
    print("✅ test_citation_verification_disclosure_flags_invalid_with_neutral_wording_and_excludes_missing 通过")


def test_citation_verification_disclosure_noop_when_fetched_sources_empty():
    """场景d：fetched_sources为空 -> 报告保持原样，不产生误报。"""
    report = "参考 https://anything.com/x 得出结论。"
    result = dr._append_citation_verification_disclosure(report, [])
    assert result == report, (
        "没有任何真实检索记录可做比对依据时，不应该产出任何可能误导的结论"
    )
    print("✅ test_citation_verification_disclosure_noop_when_fetched_sources_empty 通过")


# ===========================================================================
# 第五部分（强制要求）：并行researcher的降级事件合并，必须跑通真实编译的
# LangGraph 图（researcher_subgraph + supervisor_subgraph），不能只测reducer
# 函数本身。同时覆盖"supervisor子图一进来父状态就已经有事件"这个场景
# （见审批意见第3点）。
# ===========================================================================

_RESEARCH_PROMPT_PREFIX = research_system_prompt.split("{date}")[0]
_FINAL_REPORT_PROMPT_PREFIX = final_report_generation_prompt.split("{research_brief}")[0]

TOPIC_A = "Topic A: internal doc availability (fails)"
TOPIC_B = "Topic B: harmless background research (succeeds)"
TOPIC_C = "Topic C: persistent infrastructure failure (circuit breaker escalation)"


def _topic_from_messages(messages):
    for m in messages:
        if isinstance(m, HumanMessage) and m.content in (TOPIC_A, TOPIC_B, TOPIC_C):
            return m.content
    return None


class _ContentAwareFakeChatModel(BaseChatModel):
    """按输入消息内容判断该返回什么的假模型。并发安全——每次调用只看自己
    的输入messages，不依赖任何共享可变状态（不像GenericFakeChatModel那样
    是个固定顺序的迭代器，多个并行调用者共享时谁先谁后不确定）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "content-aware-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        # 生成逻辑完全不看bind了哪些tools，只是让.bind_tools()这个调用链
        # 不崩——真正"该调用哪个工具"的决定在_decide里按消息结构判断。
        return self.bind(tools=tools)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        ai_message = self._decide(messages)
        return ChatResult(generations=[ChatGeneration(message=ai_message)])

    def _decide(self, messages) -> AIMessage:
        def new_id():
            return f"call_{uuid.uuid4().hex[:8]}"

        # 1) final_report_generation：单条HumanMessage，匹配报告prompt的静态前缀
        if (len(messages) == 1 and isinstance(messages[0], HumanMessage)
                and messages[0].content.startswith(_FINAL_REPORT_PROMPT_PREFIX)):
            return AIMessage(content="FAKE FINAL REPORT BODY")

        # 2) compress_research：最后一条精确匹配压缩指令（这条消息没有占位符，全字匹配）
        if messages and isinstance(messages[-1], HumanMessage) and messages[-1].content == compress_research_simple_human_message:
            topic = _topic_from_messages(messages) or "?"
            return AIMessage(content=f"COMPRESSED[{topic}]")

        # 3) researcher：第一条是SystemMessage，匹配研究者prompt的静态前缀
        if messages and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(_RESEARCH_PROMPT_PREFIX):
            topic = _topic_from_messages(messages)
            tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
            if topic == TOPIC_A:
                if tool_msg_count == 0:
                    return AIMessage(content="", tool_calls=[
                        {"name": "internal_doc_search", "args": {"query": "roadmap"}, "id": new_id()}
                    ])
                return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": new_id()}])
            elif topic == TOPIC_C:
                # 持续重复调用同一个确定性失败的工具，直到看到至少3条
                # ToolMessage（2次retries_exhausted + 1次circuit_breaker_open），
                # 才改口ResearchComplete——用来驱动真实图里完整的
                # "连续失败->熔断"升级路径，不是只测单次失败。
                if tool_msg_count < 3:
                    return AIMessage(content="", tool_calls=[
                        {"name": "internal_doc_search", "args": {"query": "roadmap"}, "id": new_id()}
                    ])
                return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": new_id()}])
            else:
                if tool_msg_count == 0:
                    return AIMessage(content="", tool_calls=[
                        {"name": "think_tool", "args": {"reflection": "planning"}, "id": new_id()}
                    ])
                return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": new_id()}])

        # 4) 剩下的情况（不匹配上面任何已知结构）：supervisor
        tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
        if tool_msg_count == 0:
            return AIMessage(content="", tool_calls=[
                {"name": "ConductResearch", "args": {"research_topic": TOPIC_A}, "id": new_id()},
                {"name": "ConductResearch", "args": {"research_topic": TOPIC_B}, "id": new_id()},
            ])
        return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": new_id()}])


async def test_full_graph_parallel_researchers_degradation_merge_and_disclosure():
    tmp_chroma_dir = tempfile.mkdtemp(prefix="t3_empty_chroma_")
    try:
        fake_model = _ContentAwareFakeChatModel()
        config = {"configurable": {
            "tool_retry_max_attempts": 2,
            "tool_retry_base_delay_seconds": 0.01,
            "tool_circuit_breaker_threshold": 3,
            "tool_call_timeout_seconds": 10.0,
        }}

        # 模拟"父状态进入supervisor子图时已经携带了之前某一轮的事件"——
        # 审批意见第3点明确要求覆盖这个场景，不能假设每次都是从空列表开始。
        preexisting_event = {
            "type": "source_unavailable", "source": "internal_doc_search",
            "reason": "retries_exhausted", "failure_kind": "infrastructure_error",
            "attempts": 1, "research_topic": "PRIOR ROUND TOPIC",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        initial_state = {
            "supervisor_messages": [HumanMessage(content="SUPERVISOR_BRIEF_MARKER_T3TEST")],
            "research_brief": "",
            "research_iterations": 0,
            "degradation_events": [preexisting_event],
            "fetched_sources": [],
        }

        # internal_doc_search 的chroma collection在这个空目录里不存在，
        # get_collection会确定性地抛NotFoundError——不是TRANSIENT_NETWORK_
        # EXCEPTION_TYPES，走execute_tool_safely的generic except分支，单次
        # 尝试立刻返回infrastructure_error，不需要重试也不需要真的发网络请求。
        with patch.object(dr, "configurable_model", fake_model), \
             patch.object(ids_mod, "CHROMA_DIR", tmp_chroma_dir):
            final_supervisor_state = await dr.supervisor_subgraph.ainvoke(initial_state, config)

            events = final_supervisor_state["degradation_events"]
            assert preexisting_event in events, "父状态已有的事件在真实图运行完之后必须被保留，不能丢失"

            events_a = [e for e in events if e["research_topic"] == TOPIC_A]
            events_b = [e for e in events if e["research_topic"] == TOPIC_B]
            assert len(events_a) == 1, f"TopicA研究者（internal_doc_search确定性失败）应该恰好产生1条事件，实际: {events_a}"
            assert events_a[0]["reason"] == "retries_exhausted"
            assert events_a[0]["source"] == "internal_doc_search"
            assert events_b == [], "TopicB研究者（think_tool成功）不应该产生任何降级事件"
            assert len(events) == 2, f"应该恰好是1条预置+1条TopicA新增，实际总数{len(events)}: {events}"

            # 同一个假模型喂给final_report_generation，验证事件同时出现在
            # 最终报告文本里（不只是state里有，报告正文也要能看到）
            agent_state = {
                "messages": [],
                "research_brief": "test brief",
                "notes": [],
                "degradation_events": events,
            }
            report_result = await dr.final_report_generation(agent_state, config)

        report_text = report_result["final_report"]
        assert dr._DEGRADATION_SECTION_MARKER in report_text
        assert "internal_doc_search" in report_text
        assert "PRIOR ROUND TOPIC" in report_text
        assert TOPIC_A in report_text

        print("✅ test_full_graph_parallel_researchers_degradation_merge_and_disclosure 通过"
              f"（真实编译图：TopicA产生1条降级事件，TopicB零事件，父状态预置事件被保留，"
              f"最终报告披露小节包含预期内容；合计{len(events)}条事件）")
    finally:
        shutil.rmtree(tmp_chroma_dir, ignore_errors=True)


async def test_full_graph_circuit_breaker_escalation_sequence_and_disclosure_dedup():
    """熔断阈值调低到2，假工具（internal_doc_search，经由Chroma集合不存在
    这条确定性失败路径）连续多轮被真实调用失败，模型连续多轮重复调用同一
    工具（TOPIC_C的假模型逻辑）——验证真实图里完整走出
    retries_exhausted -> retries_exhausted -> circuit_breaker_open 这条
    升级序列，而不是只测单次失败或者人为构造一个已经达到阈值的初始计数。
    同时验证披露小节去重后，两条reason相同的retries_exhausted被合并成一条，
    circuit_breaker_open单独保留一条。
    """
    tmp_chroma_dir = tempfile.mkdtemp(prefix="t3_cb_escalation_chroma_")
    try:
        fake_model = _ContentAwareFakeChatModel()
        config = {"configurable": {
            "tool_retry_max_attempts": 2,
            "tool_retry_base_delay_seconds": 0.01,
            "tool_circuit_breaker_threshold": 2,  # 故意调低，2次真实失败就应该熔断
            "tool_call_timeout_seconds": 10.0,
            "max_react_tool_calls": 10,
        }}
        state = {
            "researcher_messages": [HumanMessage(content=TOPIC_C)],
            "research_topic": TOPIC_C,
        }
        with patch.object(dr, "configurable_model", fake_model), \
             patch.object(ids_mod, "CHROMA_DIR", tmp_chroma_dir):
            final_state = await dr.researcher_subgraph.ainvoke(state, config)

        events = final_state["degradation_events"]
        assert len(events) == 3, f"应该恰好是2次retries_exhausted+1次circuit_breaker_open，实际: {events}"
        assert [e["reason"] for e in events] == [
            "retries_exhausted", "retries_exhausted", "circuit_breaker_open",
        ], f"必须严格按时间顺序先出现两次retries_exhausted，再出现circuit_breaker_open，实际顺序: {[e['reason'] for e in events]}"
        assert events[0]["attempts"] == 1 and events[1]["attempts"] == 1, (
            "每次都是Chroma集合不存在这个单次失败的确定性路径（不走TRANSIENT_NETWORK_"
            "EXCEPTION_TYPES的重试分支），attempts应该都是1"
        )
        assert events[2]["attempts"] == 0, "熔断拦截时工具根本没被真的调用，attempts必须是0"
        assert all(e["source"] == "internal_doc_search" and e["research_topic"] == TOPIC_C for e in events)

        # 披露小节去重：3条原始事件里，前两条source+topic+reason完全一样，
        # 应该被合并成1条；第3条reason不同，单独保留——去重后应该是2条，不是3条。
        deduped = dr._dedupe_degradation_events(events)
        assert len(deduped) == 2, f"去重后应该是retries_exhausted一条+circuit_breaker_open一条，实际: {deduped}"
        disclosure = dr._append_degradation_disclosure("# 报告正文", events)
        assert disclosure.count("internal_doc_search") == 2, (
            "披露小节里internal_doc_search应该恰好出现2次（去重后的retries_exhausted"
            "一条+circuit_breaker_open一条），不是3次"
        )

        print("✅ test_full_graph_circuit_breaker_escalation_sequence_and_disclosure_dedup 通过"
              f"（真实编译图：阈值2，真实观察到 retries_exhausted→retries_exhausted→"
              f"circuit_breaker_open 的完整升级序列，披露小节去重后从3条合并为2条）")
    finally:
        shutil.rmtree(tmp_chroma_dir, ignore_errors=True)


async def main():
    await test_circuit_breaker_open_records_degradation_event_with_zero_attempts()
    await test_retries_exhausted_records_degradation_event_with_correct_attempts()
    await test_outer_timeout_records_degradation_event_with_attempts_none()
    await test_tool_input_error_does_not_record_degradation_event()
    await test_successful_call_does_not_record_degradation_event()
    await test_unknown_tool_name_does_not_record_degradation_event()
    await test_fetched_sources_extracted_only_on_success_status()
    await test_fetched_sources_extraction_against_real_tavily_formatter_output()
    await test_fetched_sources_extraction_does_not_crash_on_non_string_or_url_less_content()
    await test_fetched_sources_not_extracted_when_status_is_error_even_if_content_mentions_url()
    await test_supervisor_tools_merges_parallel_degradation_events_preserving_topic_via_extend()
    await test_supervisor_tools_merges_parallel_fetched_sources_via_extend()
    await test_supervisor_tools_omits_keys_when_no_new_events_present()
    test_dedupe_degradation_events_by_source_topic_reason_ignores_timestamp()
    test_append_degradation_disclosure_noop_on_empty_events()
    test_append_degradation_disclosure_noop_if_marker_already_present()
    test_append_degradation_disclosure_adds_expected_section()
    test_outer_timeout_disclosure_wording_does_not_assert_unavailable()
    test_build_degradation_prompt_hint_empty_and_nonempty()
    test_normalize_url_rules()
    test_verify_citations_valid_invalid_missing_categories()
    test_citation_verification_disclosure_noop_when_all_traceable()
    test_citation_verification_disclosure_noop_when_only_missing_but_verify_citations_still_returns_it()
    test_citation_verification_disclosure_flags_invalid_with_neutral_wording_and_excludes_missing()
    test_citation_verification_disclosure_noop_when_fetched_sources_empty()
    await test_full_graph_parallel_researchers_degradation_merge_and_disclosure()
    await test_full_graph_circuit_breaker_escalation_sequence_and_disclosure_dedup()
    print("\n全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
