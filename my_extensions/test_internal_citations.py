"""T3b（内部文档 doc_id 引用的可追溯性核验）的自动化测试。

只证明“报告里引用的 doc_id 本次研究确实通过 internal_doc_search 检索到过”
（来源可追溯），不证明文档内容支持报告里的结论。独立脚本，风格同
test_degradation_disclosure.py。
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import shutil
import textwrap
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from open_deep_research import citation_verification as cv
from open_deep_research import deep_researcher as dr
from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS
import test_degradation_disclosure as td

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

TWO_CHUNKS = (
    "[Source: doc01_synthmotion_intro, section: Overview, relevance_distance: 0.123]\nbody one"
    "\n\n---\n\n"
    "[Source: doc02_market_report, section: Q3, relevance_distance: 0.456]\nbody two"
)


def _ids(entries):
    return [e["doc_id"] for e in entries]


# ---------------- 第一部分：提取（extract_internal_doc_ids） ----------------

def test_extract_two_chunks_in_order():
    assert cv.extract_internal_doc_ids(TWO_CHUNKS) == ["doc01_synthmotion_intro", "doc02_market_report"]
    print("✅ test_extract_two_chunks_in_order 通过")


def test_extract_dedupes_same_doc_across_chunks():
    out = (
        "[Source: doc01_a, section: S1, relevance_distance: 0.100]\nx"
        "\n\n---\n\n"
        "[Source: doc01_a, section: S2, relevance_distance: 0.200]\ny"
    )
    assert cv.extract_internal_doc_ids(out) == ["doc01_a"]
    print("✅ test_extract_dedupes_same_doc_across_chunks 通过")


def test_extract_ignores_marker_embedded_in_chunk_body():
    out = (
        "[Source: doc01_a, section: S, relevance_distance: 0.100]\n"
        "正文里恰好有一行\n[Source: doc99_fake, section: X, relevance_distance: 0.000]\n还有别的"
    )
    assert cv.extract_internal_doc_ids(out) == ["doc01_a"], "只解析每个chunk的首行，正文里的标记不能算"
    print("✅ test_extract_ignores_marker_embedded_in_chunk_body 通过")


def test_extract_no_results_message_yields_nothing():
    assert cv.extract_internal_doc_ids("No relevant documents found in the internal knowledge base.") == []
    assert cv.extract_internal_doc_ids("") == []
    print("✅ test_extract_no_results_message_yields_nothing 通过")


def test_extract_malformed_header_yields_nothing():
    assert cv.extract_internal_doc_ids("[Source: doc01_a]\nbody") == []
    assert cv.extract_internal_doc_ids("Document search error: boom") == []
    print("✅ test_extract_malformed_header_yields_nothing 通过")


# ---------------- 第二部分：核验（verify_doc_id_citations） ----------------

FETCHED = [
    {"source": "internal_doc_search", "doc_id": "doc01_synthmotion_intro", "research_topic": "t"},
    {"source": "internal_doc_search", "doc_id": "doc02_market_report", "research_topic": "t"},
]


def test_verify_valid_invalid_missing():
    report = "见 [Source: doc01_synthmotion_intro, section: A] 和 [Source: doc07_never_seen]。"
    v = cv.verify_doc_id_citations(report, FETCHED)
    assert v["valid"] == ["doc01_synthmotion_intro"], v
    assert v["invalid"] == ["doc07_never_seen"], v
    assert v["missing"] == ["doc02_market_report"], v
    print("✅ test_verify_valid_invalid_missing 通过")


def test_verify_case_insensitive_and_md_suffix():
    v = cv.verify_doc_id_citations("[Source: DOC01_SynthMotion_Intro.md]", FETCHED)
    assert v["invalid"] == [] and len(v["valid"]) == 1, v
    print("✅ test_verify_case_insensitive_and_md_suffix 通过")


def test_verify_heuristic_bare_doc_id_in_prose():
    v = cv.verify_doc_id_citations("根据 doc02_market_report 的结论……", FETCHED)
    assert v["valid"] == ["doc02_market_report"] and v["invalid"] == [], v
    v2 = cv.verify_doc_id_citations("根据 doc09_phantom 的结论……", FETCHED)
    assert v2["invalid"] == ["doc09_phantom"], v2
    print("✅ test_verify_heuristic_bare_doc_id_in_prose 通过")


def test_verify_truncated_id_is_a_known_limitation_not_matched():
    # 已知局限（如实记录）：模型把 id 截短/改写后，既不算 valid 也可能不被识别为引用。
    v = cv.verify_doc_id_citations("[Source: doc01_synthmotion]", FETCHED)
    assert v["valid"] == [], "截断的 id 不应被当作 valid"
    assert v["invalid"] == ["doc01_synthmotion"], "截断 id 会被标 invalid（保守，需人工核实）"
    print("✅ test_verify_truncated_id_is_a_known_limitation_not_matched 通过")


def test_verify_no_false_positive_on_urls_and_plain_names():
    report = (
        "来源 [Source: https://example.com/a]、[Source: example.com]、"
        "[Source: Company Handbook]、doc01、document_01_x 都不是内部 doc_id。"
    )
    v = cv.verify_doc_id_citations(report, FETCHED)
    assert v["invalid"] == [], f"不应误报: {v}"
    print("✅ test_verify_no_false_positive_on_urls_and_plain_names 通过")


# ---------------- 第三部分：披露小节 ----------------

def test_disclosure_noop_when_no_fetched_doc_ids():
    r = "正文 [Source: doc07_never_seen]"
    assert dr._append_doc_id_citation_disclosure(r, []) == r
    print("✅ test_disclosure_noop_when_no_fetched_doc_ids 通过")


def test_disclosure_noop_when_only_missing():
    r = "正文，没有引用任何内部文档。"
    assert dr._append_doc_id_citation_disclosure(r, FETCHED) == r
    print("✅ test_disclosure_noop_when_only_missing 通过")


def test_disclosure_noop_when_all_valid():
    r = "正文 [Source: doc01_synthmotion_intro]"
    assert dr._append_doc_id_citation_disclosure(r, FETCHED) == r
    print("✅ test_disclosure_noop_when_all_valid 通过")


def test_disclosure_appended_once_and_idempotent_and_excludes_missing():
    r = "正文 [Source: doc07_never_seen] [Source: doc01_synthmotion_intro]"
    out = dr._append_doc_id_citation_disclosure(r, FETCHED)
    assert out.count(dr._DOC_ID_CITATION_SECTION_MARKER) == 1
    assert "- doc07_never_seen" in out
    assert "doc02_market_report" not in out, "missing 不能出现在披露里"
    assert "编造" not in out and "fabricat" not in out.lower(), "措辞必须中性"
    assert dr._append_doc_id_citation_disclosure(out, FETCHED) == out, "重复调用必须幂等"
    print("✅ test_disclosure_appended_once_and_idempotent_and_excludes_missing 通过")


def test_url_and_doc_disclosures_coexist_without_cross_scanning():
    fetched_urls = [{"source": "tavily_search", "url": "https://ok.com/a", "research_topic": "t"}]
    r = "正文 https://bad.com/x [Source: doc07_never_seen]"
    r = dr._append_citation_verification_disclosure(r, fetched_urls)
    r = dr._append_doc_id_citation_disclosure(r, FETCHED)
    assert r.count(dr._CITATION_VERIFICATION_SECTION_MARKER) == 1
    assert r.count(dr._DOC_ID_CITATION_SECTION_MARKER) == 1
    # 前面披露小节里的内容不能被当成报告正文里的 doc 引用
    r2 = "正文无引用\n\n" + dr._CITATION_VERIFICATION_SECTION_MARKER + "\n- doc88_in_url_section"
    assert dr._append_doc_id_citation_disclosure(r2, FETCHED) == r2
    print("✅ test_url_and_doc_disclosures_coexist_without_cross_scanning 通过")


# ---------------- 第四部分：researcher_tools 采集 ----------------

async def _run(tool_name, args, fake):
    state = td._make_state(tool_name, args, research_topic="T3B-TOPIC")
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {tool_name: fake}):
        return await dr.researcher_tools(state, {"configurable": {}})


async def test_researcher_tools_collects_from_internal_doc_search_success():
    async def fake(query):
        return TWO_CHUNKS
    res = await _run("internal_doc_search", {"query": "q"}, fake)
    got = res.update["fetched_doc_ids"]
    assert _ids(got) == ["doc01_synthmotion_intro", "doc02_market_report"], got
    assert all(e["source"] == "internal_doc_search" and e["research_topic"] == "T3B-TOPIC" for e in got)
    print("✅ test_researcher_tools_collects_from_internal_doc_search_success 通过")


async def test_researcher_tools_ignores_other_tools_even_if_output_looks_like_header():
    async def fake(code):
        return "[Source: doc66_fake, section: X, relevance_distance: 0.1]\nfrom a web page"
    res = await _run("python_executor", {"code": "x"}, fake)
    assert res.update["fetched_doc_ids"] == [], "只有 internal_doc_search 的输出才可能产生 doc_id"
    print("✅ test_researcher_tools_ignores_other_tools_even_if_output_looks_like_header 通过")


async def test_researcher_tools_no_collection_when_status_not_success():
    from open_deep_research.tool_reliability import TransientToolError

    async def fake(query):
        raise TransientToolError("Document search error: boom")
    state = td._make_state("internal_doc_search", {"query": "q"})
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": fake}):
        res = await dr.researcher_tools(state, {"configurable": {"tool_retry_max_attempts": 1,
                                                                 "tool_retry_base_delay_seconds": 0.0}})
    assert res.update["fetched_doc_ids"] == []
    print("✅ test_researcher_tools_no_collection_when_status_not_success 通过")


async def test_researcher_tools_no_results_message_collects_nothing():
    async def fake(query):
        return "No relevant documents found in the internal knowledge base."
    res = await _run("internal_doc_search", {"query": "q"}, fake)
    assert res.update["fetched_doc_ids"] == []
    print("✅ test_researcher_tools_no_results_message_collects_nothing 通过")


async def test_supervisor_tools_merges_parallel_fetched_doc_ids_and_omits_when_empty():
    async def fake_ainvoke(payload, config=None):
        topic = payload["research_topic"]
        return {"compressed_research": "s", "raw_notes": [], "degradation_events": [],
                "fetched_sources": [],
                "fetched_doc_ids": [{"source": "internal_doc_search", "doc_id": f"doc01_{topic[-1]}",
                                     "research_topic": topic}]}
    state = td._supervisor_state_with_conduct_research(["Topic 1", "Topic 2"])
    with patch.object(dr.researcher_subgraph, "ainvoke", fake_ainvoke):
        result = await dr.supervisor_tools(state, {"configurable": {}})
    got = result.update["fetched_doc_ids"]
    assert sorted(_ids(got)) == ["doc01_1", "doc01_2"], got

    async def fake_empty(payload, config=None):
        return {"compressed_research": "s", "raw_notes": [], "degradation_events": [],
                "fetched_sources": [], "fetched_doc_ids": []}
    with patch.object(dr.researcher_subgraph, "ainvoke", fake_empty):
        result = await dr.supervisor_tools(state, {"configurable": {}})
    assert "fetched_doc_ids" not in result.update
    print("✅ test_supervisor_tools_merges_parallel_fetched_doc_ids_and_omits_when_empty 通过")


# ---------------- 第五部分：真实编译图往返 ----------------

class _ReportModel(td._ContentAwareFakeChatModel):
    """与 T3 测试相同的假模型，只把最终报告正文换成带有 doc_id 引用的文本。"""

    def _decide(self, messages) -> AIMessage:
        m = super()._decide(messages)
        if m.content == "FAKE FINAL REPORT BODY":
            return AIMessage(content=(
                "# 报告\n结论A [Source: doc01_synthmotion_intro]；结论B [Source: doc42_invented]。"
            ))
        return m


async def test_full_graph_round_trip_doc_id_provenance():
    async def fake_ids(query):
        return TWO_CHUNKS
    fake_model = _ReportModel()
    config = {"configurable": {"tool_retry_max_attempts": 1, "tool_retry_base_delay_seconds": 0.0,
                               "tool_call_timeout_seconds": 10.0}}
    initial = {"supervisor_messages": [HumanMessage(content="SUPERVISOR_BRIEF_MARKER_T3BTEST")],
               "research_brief": "", "research_iterations": 0,
               "degradation_events": [], "fetched_sources": [], "fetched_doc_ids": []}
    with patch.object(dr, "configurable_model", fake_model), \
         patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": fake_ids}):
        sup = await dr.supervisor_subgraph.ainvoke(initial, config)
        got = sup["fetched_doc_ids"]
        # TopicA 研究者调用了 internal_doc_search，TopicB 没有
        assert {e["research_topic"] for e in got} == {td.TOPIC_A}, got
        assert set(_ids(got)) == {"doc01_synthmotion_intro", "doc02_market_report"}
        agent_state = {"messages": [], "research_brief": "b", "notes": [], "fetched_doc_ids": got}
        out = await dr.final_report_generation(agent_state, config)
    report = out["final_report"]
    assert report.count(dr._DOC_ID_CITATION_SECTION_MARKER) == 1, report
    assert "- doc42_invented" in report
    assert "- doc01_synthmotion_intro" not in report
    print("✅ test_full_graph_round_trip_doc_id_provenance 通过（真实编译图：采集->合并->报告披露）")


class _BothSearchModel(td._ContentAwareFakeChatModel):
    """TopicA 与 TopicB 两个并行 researcher 都调用 internal_doc_search（查询词不同）。"""

    def _decide(self, messages) -> AIMessage:
        from langchain_core.messages import SystemMessage, ToolMessage
        if (messages and isinstance(messages[0], SystemMessage)
                and messages[0].content.startswith(td._RESEARCH_PROMPT_PREFIX)
                and td._topic_from_messages(messages) == td.TOPIC_B
                and not any(isinstance(m, ToolMessage) for m in messages)):
            return AIMessage(content="", tool_calls=[
                {"name": "internal_doc_search", "args": {"query": "beta"}, "id": "call_beta1"}])
        return super()._decide(messages)


async def test_full_graph_two_parallel_researchers_both_use_internal_doc_search():
    async def fake_ids(query):
        if query == "beta":
            return "[Source: doc05_beta_notes, section: B, relevance_distance: 0.222]\nbeta body"
        return TWO_CHUNKS
    config = {"configurable": {"tool_retry_max_attempts": 1, "tool_retry_base_delay_seconds": 0.0,
                               "tool_call_timeout_seconds": 10.0}}
    initial = {"supervisor_messages": [HumanMessage(content="SUPERVISOR_BRIEF_MARKER_T3BTEST")],
               "research_brief": "", "research_iterations": 0,
               "degradation_events": [], "fetched_sources": [], "fetched_doc_ids": []}
    with patch.object(dr, "configurable_model", _BothSearchModel()), \
         patch.dict(_RETRY_AWARE_TOOL_CALLS, {"internal_doc_search": fake_ids}):
        sup = await dr.supervisor_subgraph.ainvoke(initial, config)
    got = sup["fetched_doc_ids"]
    by_topic = {}
    for e in got:
        by_topic.setdefault(e["research_topic"], []).append(e["doc_id"])
    assert by_topic.get(td.TOPIC_A) == ["doc01_synthmotion_intro", "doc02_market_report"], by_topic
    assert by_topic.get(td.TOPIC_B) == ["doc05_beta_notes"], by_topic
    assert len(got) == 3, got
    print("✅ test_full_graph_two_parallel_researchers_both_use_internal_doc_search 通过"
          "（真实编译图：两个并行 researcher 各自的 doc_id 都保留且归属正确）")


# ---------------- 第六部分：未使用内部文档工具时与 f141ce5 逐字节一致 ----------------

_DRIVER = textwrap.dedent('''
    import asyncio, json, sys, os
    sys.path.insert(0, os.environ["MYEXT"])
    from unittest.mock import patch
    import test_degradation_disclosure as td
    from open_deep_research import deep_researcher as dr

    async def go():
        model = td._ContentAwareFakeChatModel()
        cfg = {"configurable": {}}
        outs = []
        for fs in ([], [{"source": "tavily_search", "url": "https://ok.com/a", "research_topic": "t"}]):
            st = {"messages": [], "research_brief": "b", "notes": [], "fetched_sources": fs}
            with patch.object(dr, "configurable_model", model):
                r = await dr.final_report_generation(st, cfg)
            outs.append(r["final_report"])
        print(json.dumps(outs, ensure_ascii=False))
    asyncio.run(go())
''')


def _run_driver(src_dir, myext_dir, driver_path):
    env = dict(os.environ, PYTHONPATH=src_dir, MYEXT=myext_dir, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, driver_path], capture_output=True, text=True, env=env, timeout=40)
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stdout.strip().splitlines()[-1]


def test_report_identical_to_baseline_f141ce5_when_internal_doc_search_unused():
    if subprocess.run(["git", "cat-file", "-e", "f141ce5^{commit}"], cwd=REPO_ROOT,
                      capture_output=True).returncode != 0:
        print("⏭ 跳过 test_report_identical_to_baseline_f141ce5：公开仓库不含历史提交 f141ce5")
        return
    wt = tempfile.mkdtemp(prefix="t3b_baseline_wt_")
    drv_dir = tempfile.mkdtemp(prefix="t3b_driver_")
    try:
        subprocess.run(["git", "worktree", "add", "--detach", wt, "f141ce5"], cwd=REPO_ROOT,
                       check=True, capture_output=True)
        driver = os.path.join(drv_dir, "driver.py")
        with open(driver, "w") as f:
            f.write(_DRIVER)
        myext = os.path.join(REPO_ROOT, "my_extensions")
        base = _run_driver(os.path.join(wt, "src"), myext, driver)
        new = _run_driver(os.path.join(REPO_ROOT, "src"), myext, driver)
        assert base == new, "未使用 internal_doc_search 时，最终报告必须与 f141ce5 逐字节一致"
        print("✅ test_report_identical_to_baseline_f141ce5_when_internal_doc_search_unused 通过")
    finally:
        subprocess.run(["git", "worktree", "remove", "-f", "-f", wt], cwd=REPO_ROOT, capture_output=True)
        shutil.rmtree(drv_dir, ignore_errors=True)


async def main():
    test_extract_two_chunks_in_order()
    test_extract_dedupes_same_doc_across_chunks()
    test_extract_ignores_marker_embedded_in_chunk_body()
    test_extract_no_results_message_yields_nothing()
    test_extract_malformed_header_yields_nothing()
    test_verify_valid_invalid_missing()
    test_verify_case_insensitive_and_md_suffix()
    test_verify_heuristic_bare_doc_id_in_prose()
    test_verify_truncated_id_is_a_known_limitation_not_matched()
    test_verify_no_false_positive_on_urls_and_plain_names()
    test_disclosure_noop_when_no_fetched_doc_ids()
    test_disclosure_noop_when_only_missing()
    test_disclosure_noop_when_all_valid()
    test_disclosure_appended_once_and_idempotent_and_excludes_missing()
    test_url_and_doc_disclosures_coexist_without_cross_scanning()
    await test_researcher_tools_collects_from_internal_doc_search_success()
    await test_researcher_tools_ignores_other_tools_even_if_output_looks_like_header()
    await test_researcher_tools_no_collection_when_status_not_success()
    await test_researcher_tools_no_results_message_collects_nothing()
    await test_supervisor_tools_merges_parallel_fetched_doc_ids_and_omits_when_empty()
    await test_full_graph_round_trip_doc_id_provenance()
    await test_full_graph_two_parallel_researchers_both_use_internal_doc_search()
    test_report_identical_to_baseline_f141ce5_when_internal_doc_search_unused()
    print("\n全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
