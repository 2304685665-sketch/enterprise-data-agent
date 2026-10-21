"""T3（降级披露 + 引用核验）的评测数据集——19条场景，全部mock/确定性断言，
零真实API调用。

和 test_degradation_disclosure.py 的区别：那个文件是按"新增行为"切分的
单元测试（一个函数一个行为，断言失败就报traceback）；这里是按"场景→原始
输出→人工可读判定"组织的评测表，像 eval_sql_guard.py 对T1那样，逐条记录
原始证据、给出判定分类，最后按组统计并断言分组总数闭合（verdict总数==
该组case数）。这是T3审批计划第5步要求的"评测集"产物，不是对已有测试的
重复。

四组，覆盖降级披露链路的四个阶段：
  A. researcher_tools 对单次工具调用结果的降级事件分类（6条）
  B. fetched_sources 的URL提取（5条）
  C. 最终报告降级披露小节的追加/去重/措辞（4条）
  D. citation_verification 接入 final_report_generation 之后的追加逻辑（4条）

真实API复现（SynthMotion那类用真实LLM跑一遍验证模型配合度）不在本轮范围，
按要求另行审批，这里完全不碰网络/模型API。
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.dirname(__file__))

from open_deep_research import deep_researcher as dr
from open_deep_research import citation_verification as cv
from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS

RESULTS = []


def _record(case_id, group, description, raw_output, verdict, note=""):
    RESULTS.append({
        "id": case_id, "group": group, "description": description,
        "raw_output": raw_output, "verdict": verdict, "note": note,
    })
    print(f"[{case_id}] {verdict:10s} ({group}) {description}")
    if note:
        print(f"      note: {note}")


def _make_state(tool_name, args, tool_failure_counts=None, research_topic="eval-topic"):
    msg = AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "1"}])
    return {
        "researcher_messages": [msg],
        "tool_call_iterations": 0,
        "research_topic": research_topic,
        "compressed_research": "",
        "raw_notes": [],
        "tool_failure_counts": tool_failure_counts or {},
    }


# ===========================================================================
# 组A：researcher_tools 的降级事件分类（6条）
# ===========================================================================

async def run_group_a():
    # A1：正常成功，不应该产生事件
    async def ok_call(code):
        return "42"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
        result = await dr.researcher_tools(_make_state("python_executor", {"code": "x"}), {"configurable": {}})
    events = result.update["degradation_events"]
    _record("A1", "A", "工具正常成功", events, "按预期" if events == [] else "不符预期",
            "预期：不产生任何degradation_event")

    # A2：工具自己处理掉的输入/代码错误（tool_input_error），不应该产生事件
    async def input_error(code):
        return "Python Error: SyntaxError: invalid syntax"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": input_error}):
        result = await dr.researcher_tools(_make_state("python_executor", {"code": "bad"}), {"configurable": {}})
    events = result.update["degradation_events"]
    _record("A2", "A", "工具自我纠错式的输入错误", events, "按预期" if events == [] else "不符预期",
            "预期：tool_input_error不计入降级，不产生事件")

    # A3：execute_tool_safely内部重试全部耗尽，真实抛异常 -> retries_exhausted
    from open_deep_research.tool_reliability import TransientToolError
    async def always_fails(code):
        raise TransientToolError("Python Error: boom")
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": always_fails}):
        result = await dr.researcher_tools(
            _make_state("python_executor", {"code": "x"}),
            {"configurable": {"tool_retry_max_attempts": 2, "tool_retry_base_delay_seconds": 0.01}},
        )
    events = result.update["degradation_events"]
    ok = len(events) == 1 and events[0]["reason"] == "retries_exhausted" and events[0]["attempts"] == 3
    _record("A3", "A", "内部重试耗尽", events, "按预期" if ok else "不符预期",
            "预期：1条事件，reason=retries_exhausted，attempts=3（首次+2次重试）")

    # A4：外层安全超时打断 -> outer_timeout，attempts=None
    async def slow_call(code):
        await asyncio.sleep(10)
        return "too slow"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": slow_call}):
        result = await dr.researcher_tools(
            _make_state("python_executor", {"code": "x"}),
            {"configurable": {"tool_call_timeout_seconds": 0.2, "tool_retry_max_attempts": 0}},
        )
    events = result.update["degradation_events"]
    ok = len(events) == 1 and events[0]["reason"] == "outer_timeout" and events[0]["attempts"] is None
    _record("A4", "A", "外层安全超时", events, "按预期" if ok else "不符预期",
            "预期：1条事件，reason=outer_timeout，attempts=None（不确定内部跑到第几次）")

    # A5：熔断已经打开，本次调用被直接拦截 -> circuit_breaker_open，attempts=0
    result = await dr.researcher_tools(
        _make_state("internal_doc_search", {"query": "x"}, tool_failure_counts={"internal_doc_search": 3}),
        {"configurable": {"tool_circuit_breaker_threshold": 3}},
    )
    events = result.update["degradation_events"]
    ok = len(events) == 1 and events[0]["reason"] == "circuit_breaker_open" and events[0]["attempts"] == 0
    _record("A5", "A", "熔断拦截（工具根本没被真的调用）", events, "按预期" if ok else "不符预期",
            "预期：1条事件，reason=circuit_breaker_open，attempts=0")

    # A6：模型幻觉出一个不存在的工具名 -> 不产生事件（范围边界：这是"没有这个源"，不是"源不可用"）
    result = await dr.researcher_tools(
        _make_state("this_tool_does_not_exist", {"foo": "bar"}), {"configurable": {}}
    )
    events = result.update["degradation_events"]
    _record("A6", "A", "幻觉工具名", events, "按预期" if events == [] else "不符预期",
            "预期：不产生事件（刻意的范围边界，不是遗漏）")


# ===========================================================================
# 组B：fetched_sources 提取（5条）
# ===========================================================================

async def run_group_b():
    tavily_style = (
        "\n\n--- SOURCE 1: A ---\nURL: https://example.com/a\n\nSUMMARY:\nx\n\n"
        "\n\n--- SOURCE 2: B ---\nURL: https://example.com/b\n\nSUMMARY:\ny\n\n"
    )

    # B1：status=success，标准tavily格式 -> 两个URL都提取到
    async def fake_success(query, n_results=3):
        return tavily_style
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": fake_success}):
        result = await dr.researcher_tools(_make_state("internal_doc_search", {"query": "q"}), {"configurable": {}})
    sources = result.update["fetched_sources"]
    urls = {s["url"] for s in sources}
    ok = urls == {"https://example.com/a", "https://example.com/b"}
    _record("B1", "B", "status=success，标准tavily格式", sources, "按预期" if ok else "不符预期",
            "预期：提取到2个URL")

    # B2：status=error，内容恰好含"URL:"字样 -> 不应该提取
    async def fake_error(code):
        return "Python Error: failed while fetching URL: https://example.com/should-not-count"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": fake_error}):
        result = await dr.researcher_tools(_make_state("python_executor", {"code": "x"}), {"configurable": {}})
    sources = result.update["fetched_sources"]
    _record("B2", "B", "status=error但内容含URL字样", sources, "按预期" if sources == [] else "不符预期",
            "预期：空列表，error状态的内容不可信")

    # B3：非字符串内容（ResearchComplete真实工具返回的模型实例）-> 不崩，空列表
    msg = AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": "1"}])
    state = {**_make_state("ResearchComplete", {}), "researcher_messages": [msg]}
    result = await dr.researcher_tools(state, {"configurable": {}})
    sources = result.update["fetched_sources"]
    _record("B3", "B", "非字符串内容（ResearchComplete模型实例）", sources,
            "按预期" if sources == [] else "不符预期", "预期：不崩，空列表")

    # B4：成功但内容里没有任何URL -> 空列表
    async def plain_ok(code):
        return "计算结果是42"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": plain_ok}):
        result = await dr.researcher_tools(_make_state("python_executor", {"code": "x"}), {"configurable": {}})
    sources = result.update["fetched_sources"]
    _record("B4", "B", "成功但无URL", sources, "按预期" if sources == [] else "不符预期", "预期：空列表")

    # B5：真实tavily_search格式化函数产出的文本（只mock网络层）喂给提取逻辑
    from open_deep_research import utils as utils_mod

    async def fake_tavily_search_async(queries, max_results=5, topic="general", include_raw_content=True, config=None):
        return [{"query": queries[0], "results": [
            {"url": "https://real-tavily.com/x", "title": "T", "content": "c", "raw_content": None},
        ]}]

    with patch.object(utils_mod, "tavily_search_async", fake_tavily_search_async):
        result = await dr.researcher_tools(
            _make_state("tavily_search", {"queries": ["q"]}), {"configurable": {}}
        )
    sources = result.update["fetched_sources"]
    ok = {s["url"] for s in sources} == {"https://real-tavily.com/x"}
    _record("B5", "B", "真实tavily_search格式化代码路径产出的文本", sources,
            "按预期" if ok else "不符预期", "预期：提取到1个URL，走的是真实格式化函数不是手写字符串")


# ===========================================================================
# 组C：最终报告降级披露小节（4条）
# ===========================================================================

def run_group_c():
    # C1：events为空 -> 原样返回
    original = "# 报告正文"
    out = dr._append_degradation_disclosure(original, [])
    _record("C1", "C", "events为空", out, "按预期" if out == original else "不符预期", "预期：原样返回")

    # C2：标题已存在 -> 原样返回，不重复追加
    events = [{"source": "x", "research_topic": "t", "reason": "retries_exhausted"}]
    original = f"# 报告正文\n{dr._DEGRADATION_SECTION_MARKER}\n已经有这段了"
    out = dr._append_degradation_disclosure(original, events)
    _record("C2", "C", "标题已存在", out, "按预期" if out == original else "不符预期", "预期：原样返回，不重复追加")

    # C3：两条完全同source+topic+reason的事件 -> 去重后只出现一次
    events = [
        {"source": "internal_doc_search", "research_topic": "T1", "reason": "retries_exhausted", "attempts": 3, "timestamp": "t1"},
        {"source": "internal_doc_search", "research_topic": "T1", "reason": "retries_exhausted", "attempts": 3, "timestamp": "t2"},
    ]
    out = dr._append_degradation_disclosure("# 报告正文", events)
    ok = out.count("internal_doc_search") == 1
    _record("C3", "C", "同source+topic+reason的重复事件", out, "按预期" if ok else "不符预期",
            "预期：去重后internal_doc_search只出现1次")

    # C4：outer_timeout事件的措辞不应该断言"不可用"
    events = [{"source": "python_executor", "research_topic": "T1", "reason": "outer_timeout",
               "attempts": None, "timestamp": "t1"}]
    out = dr._append_degradation_disclosure("# 报告正文", events)
    ok = "不可用" not in out and "调用失败" in out and "原因码：outer_timeout" in out
    _record("C4", "C", "outer_timeout措辞", out, "按预期" if ok else "不符预期",
            "预期：不出现'不可用'，出现'调用失败'和原因码")


# ===========================================================================
# 组D：citation_verification 接入 final_report_generation（4条）
# ===========================================================================

def run_group_d():
    # D1（场景a）：全部可追溯 -> 不追加
    fetched = [{"source": "tavily_search", "url": "https://real.com/a", "research_topic": "T1"}]
    report = "参考 https://real.com/a 得出结论。"
    out = dr._append_citation_verification_disclosure(report, fetched)
    _record("D1", "D", "全部可追溯", out, "按预期" if out == report else "不符预期", "预期：不追加，原样返回")

    # D2（场景c）：存在invalid（且同时存在missing）-> 追加，措辞中性，
    # 且小节里不列出missing
    report2 = "报告只引用了一个模型编造的来源 https://fabricated.com/x ，没有提到其它任何链接。"
    out = dr._append_citation_verification_disclosure(report2, fetched)
    section = out.split(dr._CITATION_VERIFICATION_SECTION_MARKER)[1] if dr._CITATION_VERIFICATION_SECTION_MARKER in out else ""
    ok = (
        dr._CITATION_VERIFICATION_SECTION_MARKER in out
        and "https://fabricated.com/x" in section
        and "未能在本次检索记录中匹配到（匹配规则较保守，请自行核实）" in section
        and "可能是生成报告时产生的错误" not in out
        and "https://real.com/a" not in section
    )
    _record("D2", "D", "存在invalid（同时存在missing）", out, "按预期" if ok else "不符预期",
            "预期：追加小节，中性措辞，且小节里不出现missing（real.com/a）")

    # D3（场景b）：报告没有引用任何URL（只有missing，没有invalid）->
    # 最终报告不追加，但verify_citations自己的返回值仍然包含missing
    report3 = "这是一份完全没有引用任何链接的报告正文。"
    verification3 = cv.verify_citations(report3, fetched)
    out = dr._append_citation_verification_disclosure(report3, fetched)
    ok = (
        out == report3
        and verification3["invalid"] == []
        and {m["url"] for m in verification3["missing"]} == {"https://real.com/a"}
    )
    _record("D3", "D", "只有missing（无invalid）", {"appended_report": out, "verify_citations_missing": verification3["missing"]},
            "按预期" if ok else "不符预期",
            "预期：最终报告不追加（== report3），但verify_citations返回值仍包含missing")

    # D4（场景d）：fetched_sources为空 -> 不追加（没有依据做任何判断）
    out = dr._append_citation_verification_disclosure(report2, [])
    _record("D4", "D", "fetched_sources为空", out, "按预期" if out == report2 else "不符预期",
            "预期：不追加，没有检索记录就不下任何结论")


async def main():
    await run_group_a()
    await run_group_b()
    run_group_c()
    run_group_d()

    print(f"\n{'='*20} 分组统计 {'='*20}")
    totals = {"A": 6, "B": 5, "C": 4, "D": 4}
    all_closed = True
    for group, total in totals.items():
        group_results = [r for r in RESULTS if r["group"] == group]
        counts = {}
        for r in group_results:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print(f"--- 组{group}（共{total}条） ---  {counts}")
        closed = sum(counts.values()) == total == len(group_results)
        all_closed = all_closed and closed
        assert closed, f"组{group}统计没有闭合！"

    mismatches = [r for r in RESULTS if r["verdict"] != "按预期"]
    print(f"\n总计 {len(RESULTS)} 条，按预期 {len(RESULTS) - len(mismatches)} 条，不符预期 {len(mismatches)} 条")
    assert len(RESULTS) == 19, f"应该恰好19条，实际{len(RESULTS)}条"

    out_dir = os.path.join(os.path.dirname(__file__), "eval_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "degradation_t3_eval.json")
    with open(out_path, "w") as f:
        json.dump(RESULTS, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n结果已写入 {out_path}（已在.gitignore里，不会被提交）")

    if mismatches:
        print("\n存在不符预期的case，需要人工复核：")
        for r in mismatches:
            print(f"  [{r['id']}] {r['description']}: {r['raw_output']}")
    else:
        print("\n全部19条均按预期。")


if __name__ == "__main__":
    asyncio.run(main())
