"""B3（本地执行轨迹记录）的自动化测试。

延续本项目既有测试风格：独立脚本，每个测试是一个独立函数，main()依次跑，
不依赖pytest-asyncio。全部用临时目录，不写入项目目录本身。
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.dirname(__file__))

from open_deep_research import deep_researcher as dr
from open_deep_research import internal_doc_search as ids_mod
from open_deep_research import run_trace
from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS
import summarize_trace
from test_degradation_disclosure import _ContentAwareFakeChatModel, TOPIC_A, TOPIC_B, TOPIC_C


def _read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _trace_config(tmp_dir, **extra_configurable):
    configurable = {"run_trace_enabled": True, "run_trace_dir": tmp_dir, **extra_configurable}
    return {"configurable": configurable}


# ===========================================================================
# 1. 事件格式校验
# ===========================================================================

def test_event_format_has_all_required_fields():
    tmp = tempfile.mkdtemp(prefix="trace_fmt_")
    try:
        config = _trace_config(tmp)
        run_trace.emit("tool_start", config=config, research_topic="T", tool="x", attempt=1,
                        params={"a": 1})
        run_trace.emit("tool_end", config=config, research_topic="T", tool="x", attempt=1,
                        duration_ms=12.3, status="success", failure_kind="none", output="hello")
        path = os.path.join(tmp, "run_trace.jsonl")
        events = _read_jsonl(path)
        assert len(events) == 2
        required = {"run_id", "researcher_id", "seq", "ts", "event_type", "research_topic",
                     "tool", "attempt", "duration_ms", "status", "failure_kind", "reason"}
        for e in events:
            assert required.issubset(e.keys()), f"缺字段: {required - e.keys()}"
        assert "params_sha256" in events[0]
        assert "output_preview" in events[1] and "output_sha256" in events[1]
        assert events[0]["seq"] < events[1]["seq"], "seq应该单调递增"
        print("✅ test_event_format_has_all_required_fields 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 2. 参数与输出脱敏
# ===========================================================================

def test_params_and_output_are_redacted():
    tmp = tempfile.mkdtemp(prefix="trace_redact_")
    try:
        config = _trace_config(tmp)
        fake_key = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        run_trace.emit("tool_start", config=config, research_topic="T", tool="x", attempt=1,
                        params={"authorization": f"Bearer {fake_key}"})
        run_trace.emit("tool_end", config=config, research_topic="T", tool="x", attempt=1,
                        status="error", failure_kind="infrastructure_error",
                        output=f"request failed, key was {fake_key}")
        path = os.path.join(tmp, "run_trace.jsonl")
        raw_text = open(path).read()
        assert fake_key not in raw_text, "假密钥原文不应该出现在trace文件里（哈希也不行，只测原文）"
        assert "[REDACTED]" in raw_text
        events = _read_jsonl(path)
        assert "params_sha256" in events[0] and len(events[0]["params_sha256"]) == 64
        print("✅ test_params_and_output_are_redacted 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 3. 并发写入
# ===========================================================================

async def test_concurrent_writes_produce_valid_non_interleaved_lines():
    tmp = tempfile.mkdtemp(prefix="trace_concurrent_")
    try:
        config = _trace_config(tmp)

        async def _one(i):
            run_trace.emit("tool_start", config=config, research_topic="T", tool=f"tool{i % 5}",
                            attempt=1, params={"i": i})

        await asyncio.gather(*(_one(i) for i in range(40)))
        path = os.path.join(tmp, "run_trace.jsonl")
        with open(path) as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 40, f"应该恰好40行，实际{len(lines)}行"
        for line in lines:
            json.loads(line)  # 每一行必须都是合法JSON，没有交织/截断
        print("✅ test_concurrent_writes_produce_valid_non_interleaved_lines 通过（40行全部合法JSON）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 4. fail-open：不可写目录下主流程结果与关闭时完全相同
# ===========================================================================

def _make_state(tool_name, args, research_topic="eval-topic"):
    msg = AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "1"}])
    return {
        "researcher_messages": [msg], "tool_call_iterations": 0,
        "research_topic": research_topic, "compressed_research": "",
        "raw_notes": [], "tool_failure_counts": {},
    }


async def test_fail_open_unwritable_dir_does_not_change_main_flow_result():
    async def ok_call(code):
        return "42"

    # 制造一个必定无法创建的目录：用一个"文件"当父目录段
    blocking_file = tempfile.NamedTemporaryFile(delete=False)
    blocking_file.close()
    unwritable_dir = os.path.join(blocking_file.name, "subdir")

    state = _make_state("python_executor", {"code": "print(42)"})
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
        result_disabled = await dr.researcher_tools(state, {"configurable": {}})
        result_trace_broken = await dr.researcher_tools(
            state, _trace_config(unwritable_dir)
        )
    os.unlink(blocking_file.name)

    def _strip_messages(r):
        return {k: v for k, v in r.update.items() if k != "researcher_messages"}

    assert _strip_messages(result_disabled) == _strip_messages(result_trace_broken), (
        "trace目录不可写时，主流程的返回结果必须和关闭追踪时完全一致（fail-open）"
    )
    msgs_a = result_disabled.update["researcher_messages"]
    msgs_b = result_trace_broken.update["researcher_messages"]
    assert [m.content for m in msgs_a] == [m.content for m in msgs_b]
    assert [m.status for m in msgs_a] == [m.status for m in msgs_b]
    print("✅ test_fail_open_unwritable_dir_does_not_change_main_flow_result 通过")


# ===========================================================================
# 5. 关闭时零文件
# ===========================================================================

async def test_disabled_tracing_creates_zero_files():
    tmp = tempfile.mkdtemp(prefix="trace_disabled_")
    try:
        async def ok_call(code):
            return "42"
        state = _make_state("python_executor", {"code": "print(42)"})
        with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
            await dr.researcher_tools(state, {"configurable": {"run_trace_dir": tmp}})  # 没开enabled
        assert os.listdir(tmp) == [], "关闭追踪时不应该创建任何文件，即使配置了run_trace_dir"
        print("✅ test_disabled_tracing_creates_zero_files 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 6. 真实编译图复现TOPIC_C场景（阈值2），仅凭trace文件还原事件顺序；
#    同时验证单个researcher内部跨多轮，researcher_id全程保持不变（3a）。
#    这次改成走supervisor_tools分发（而不是直接调researcher_subgraph），
#    因为researcher_id只有经过supervisor_tools的分发循环才会被真的生成——
#    之前直接调researcher_subgraph.ainvoke时researcher_id全程是空字符串，
#    "保持不变"这个结论是空对空的trivial true，没有实际验证意义。
# ===========================================================================

async def test_topic_c_real_graph_trace_reproduces_failure_sequence_with_stable_researcher_id():
    tmp_chroma = tempfile.mkdtemp(prefix="trace_topicc_chroma_")
    tmp_trace = tempfile.mkdtemp(prefix="trace_topicc_trace_")
    try:
        fake_model = _ContentAwareFakeChatModel()
        config = _trace_config(tmp_trace,
            tool_retry_max_attempts=2, tool_retry_base_delay_seconds=0.01,
            tool_circuit_breaker_threshold=2, tool_call_timeout_seconds=10.0,
            max_react_tool_calls=10,
        )
        tool_calls = [{"name": "ConductResearch", "args": {"research_topic": TOPIC_C}, "id": "c1"}]
        state = {
            "supervisor_messages": [AIMessage(content="", tool_calls=tool_calls)],
            "research_brief": "brief", "research_iterations": 1, "notes": [], "raw_notes": [],
        }
        with patch.object(dr, "configurable_model", fake_model), \
             patch.object(ids_mod, "CHROMA_DIR", tmp_chroma):
            result = await dr.supervisor_tools(state, config)

        path = os.path.join(tmp_trace, "run_trace.jsonl")
        events = _read_jsonl(path)
        degradation_events_in_trace = [e for e in events if e["event_type"] == "degradation"]
        reasons_in_order = [e["reason"] for e in degradation_events_in_trace]
        assert reasons_in_order == ["retries_exhausted", "retries_exhausted", "circuit_breaker_open"], (
            f"仅凭trace文件应该能还原出这个顺序，实际: {reasons_in_order}"
        )
        assert len(degradation_events_in_trace) == len(result.update.get("degradation_events", [])), (
            "trace里的degradation事件数必须和最终状态里的degradation_events数一致"
        )
        breaker_blocks = [e for e in events if e["event_type"] == "breaker_block"]
        assert len(breaker_blocks) == 1, "熔断拦截应该恰好发生1次（第3轮）"

        # 3a：单个researcher内部跨多轮（这里是3轮：失败/失败/熔断拦截），
        # researcher_id应该全程只有1个值，不应该每轮都换一个。
        researcher_ids = {e["researcher_id"] for e in events if e.get("researcher_id")}
        assert researcher_ids, "应该至少有一个非空researcher_id（经过了supervisor_tools的分发）"
        assert len(researcher_ids) == 1, (
            f"单个researcher内部跨多轮，researcher_id必须保持不变，实际出现了{len(researcher_ids)}个不同值: {researcher_ids}"
        )
        print("✅ test_topic_c_real_graph_trace_reproduces_failure_sequence_with_stable_researcher_id 通过"
              f"（trace文件还原顺序: {reasons_in_order}；3轮全程researcher_id不变）")
    finally:
        shutil.rmtree(tmp_chroma, ignore_errors=True)
        shutil.rmtree(tmp_trace, ignore_errors=True)


# ===========================================================================
# 7. summarize_trace.py 对固定夹具的统计结果正确
# ===========================================================================

def test_summarize_trace_on_fixed_fixture():
    tmp = tempfile.mkdtemp(prefix="trace_summarize_")
    try:
        path = os.path.join(tmp, "fixture.jsonl")
        fixture_events = [
            {"run_id": "", "researcher_id": "r1", "seq": 0, "ts": "t", "event_type": "tool_start",
             "research_topic": "T", "tool": "a", "attempt": 1, "duration_ms": None, "status": None,
             "failure_kind": None, "reason": None},
            {"run_id": "", "researcher_id": "r1", "seq": 1, "ts": "t", "event_type": "tool_end",
             "research_topic": "T", "tool": "a", "attempt": 1, "duration_ms": 100.0, "status": "success",
             "failure_kind": "none", "reason": None},
            {"run_id": "", "researcher_id": "r1", "seq": 2, "ts": "t", "event_type": "tool_start",
             "research_topic": "T", "tool": "a", "attempt": 2, "duration_ms": None, "status": None,
             "failure_kind": None, "reason": None},
            {"run_id": "", "researcher_id": "r1", "seq": 3, "ts": "t", "event_type": "tool_end",
             "research_topic": "T", "tool": "a", "attempt": 2, "duration_ms": 200.0, "status": "error",
             "failure_kind": "infrastructure_error", "reason": None},
            {"run_id": "", "researcher_id": "r1", "seq": 4, "ts": "t", "event_type": "retry",
             "research_topic": "T", "tool": "a", "attempt": 2, "duration_ms": None, "status": None,
             "failure_kind": None, "reason": "transient_error"},
            {"run_id": "", "researcher_id": "r1", "seq": 5, "ts": "t", "event_type": "breaker_block",
             "research_topic": "T", "tool": "a", "attempt": None, "duration_ms": None, "status": "error",
             "failure_kind": "infrastructure_error", "reason": "circuit_breaker_open"},
            {"run_id": "", "researcher_id": "r1", "seq": 6, "ts": "t", "event_type": "degradation",
             "research_topic": "T", "tool": "a", "attempt": 0, "duration_ms": None, "status": "error",
             "failure_kind": "infrastructure_error", "reason": "circuit_breaker_open"},
            # 故意加一条没有对应tool_end的tool_start，验证"未配对"统计不被隐藏
            {"run_id": "", "researcher_id": "r1", "seq": 7, "ts": "t", "event_type": "tool_start",
             "research_topic": "T", "tool": "b", "attempt": 1, "duration_ms": None, "status": None,
             "failure_kind": None, "reason": None},
        ]
        with open(path, "w") as f:
            for e in fixture_events:
                f.write(json.dumps(e) + "\n")

        result = summarize_trace.summarize(path)
        assert result["total_events"] == 8
        assert result["tool_call_counts"] == {"a": 2, "b": 1}
        assert result["failure_kind_distribution"] == {"none": 1, "infrastructure_error": 1}
        assert result["retry_count"] == 1
        assert result["breaker_block_count"] == 1
        assert result["degradation_count"] == 1
        assert result["duration_ms_by_tool"]["a"]["count"] == 2
        assert result["duration_ms_by_tool"]["a"]["p50_ms"] == 150.0
        assert result["unpaired_tool_start_count"] == 1, "工具b的tool_start没有对应tool_end，必须被统计出来"
        assert result["unpaired_tool_end_count"] == 0, "这个夹具里没有构造未配对的tool_end，应该是0"
        print("✅ test_summarize_trace_on_fixed_fixture 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_summarize_trace_detects_unpaired_tool_end():
    """构造一条完全没有对应tool_start的tool_end（比如trace文件被截断、
    或者tool_start那条因为某种原因没写进去）——必须被统计成"未配对的
    tool_end"，不能被配对算法悄悄吃掉或者误配到别的tool_start上。
    """
    tmp = tempfile.mkdtemp(prefix="trace_unpaired_end_")
    try:
        path = os.path.join(tmp, "fixture.jsonl")
        fixture_events = [
            {"run_id": "", "researcher_id": "r1", "call_id": "c1", "seq": 0, "ts": "t",
             "event_type": "tool_end", "research_topic": "T", "tool": "a", "attempt": 1,
             "duration_ms": 50.0, "status": "success", "failure_kind": "none", "reason": None},
        ]
        with open(path, "w") as f:
            for e in fixture_events:
                f.write(json.dumps(e) + "\n")

        result = summarize_trace.summarize(path)
        assert result["unpaired_tool_end_count"] == 1, f"应该恰好1条未配对的tool_end，实际: {result}"
        assert result["unpaired_tool_start_count"] == 0
        print("✅ test_summarize_trace_detects_unpaired_tool_end 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_summarize_trace_loop_detected_counts_and_reason_distribution():
    """B1审查后新增：固定夹具里放3条loop_detected事件（2条repeat、1条
    force_stop），确认总数和按reason分布的统计都正确，不是只统计总数
    漏了分布，也不是分布对了总数却算错。"""
    tmp = tempfile.mkdtemp(prefix="trace_loop_detected_fixture_")
    try:
        path = os.path.join(tmp, "fixture.jsonl")
        fixture_events = [
            {"run_id": "", "researcher_id": "r1", "call_id": "", "seq": 0, "ts": "t",
             "event_type": "loop_detected", "research_topic": "T", "tool": "a", "attempt": None,
             "duration_ms": None, "status": None, "failure_kind": None, "reason": "repeat"},
            {"run_id": "", "researcher_id": "r1", "call_id": "", "seq": 1, "ts": "t",
             "event_type": "loop_detected", "research_topic": "T", "tool": "a", "attempt": None,
             "duration_ms": None, "status": None, "failure_kind": None, "reason": "repeat"},
            {"run_id": "", "researcher_id": "r1", "call_id": "", "seq": 2, "ts": "t",
             "event_type": "loop_detected", "research_topic": "T", "tool": None, "attempt": None,
             "duration_ms": None, "status": None, "failure_kind": None, "reason": "force_stop"},
        ]
        with open(path, "w") as f:
            for e in fixture_events:
                f.write(json.dumps(e) + "\n")

        result = summarize_trace.summarize(path)
        assert result["loop_detected_count"] == 3, f"应该恰好3条loop_detected，实际: {result['loop_detected_count']}"
        assert result["loop_detected_by_reason"] == {"repeat": 2, "force_stop": 1}, (
            f"按reason分布应该是repeat:2, force_stop:1，实际: {result['loop_detected_by_reason']}"
        )
        print("✅ test_summarize_trace_loop_detected_counts_and_reason_distribution 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 8. 真实编译图：两个并行researcher的事件各自归属到不同researcher_id，
#    且与各自research_topic一致
# ===========================================================================

async def test_parallel_researchers_get_distinct_researcher_ids_matching_their_topics():
    from langchain_core.messages import HumanMessage
    tmp_chroma = tempfile.mkdtemp(prefix="trace_parallel_chroma_")
    tmp_trace = tempfile.mkdtemp(prefix="trace_parallel_trace_")
    try:
        fake_model = _ContentAwareFakeChatModel()
        config = _trace_config(tmp_trace,
            tool_retry_max_attempts=2, tool_retry_base_delay_seconds=0.01,
            tool_circuit_breaker_threshold=3, tool_call_timeout_seconds=10.0,
        )
        tool_calls = [
            {"name": "ConductResearch", "args": {"research_topic": TOPIC_A}, "id": "c1"},
            {"name": "ConductResearch", "args": {"research_topic": TOPIC_B}, "id": "c2"},
        ]
        state = {
            "supervisor_messages": [AIMessage(content="", tool_calls=tool_calls)],
            "research_brief": "brief", "research_iterations": 1, "notes": [], "raw_notes": [],
        }
        with patch.object(dr, "configurable_model", fake_model), \
             patch.object(ids_mod, "CHROMA_DIR", tmp_chroma):
            await dr.supervisor_tools(state, config)

        path = os.path.join(tmp_trace, "run_trace.jsonl")
        events = _read_jsonl(path)
        by_researcher = {}
        for e in events:
            by_researcher.setdefault(e["researcher_id"], set()).add(e["research_topic"])

        assert "" not in by_researcher, "走supervisor_tools分发的事件都应该有非空researcher_id"
        assert len(by_researcher) == 2, f"应该恰好有2个不同的researcher_id，实际: {list(by_researcher.keys())}"
        topic_sets = list(by_researcher.values())
        assert {TOPIC_A} in topic_sets and {TOPIC_B} in topic_sets, (
            f"每个researcher_id下的事件research_topic必须只属于它自己的那个主题，不能混，实际: {topic_sets}"
        )
        print("✅ test_parallel_researchers_get_distinct_researcher_ids_matching_their_topics 通过"
              f"（2个researcher_id，分别只对应各自的research_topic）")
    finally:
        shutil.rmtree(tmp_chroma, ignore_errors=True)
        shutil.rmtree(tmp_trace, ignore_errors=True)


# ===========================================================================
# 9. 提供run_trace_run_id时，所有事件的run_id一致
# ===========================================================================

async def test_provided_run_trace_run_id_is_consistent_across_all_events():
    tmp = tempfile.mkdtemp(prefix="trace_runid_")
    try:
        async def ok_call(code):
            return "42"
        state = _make_state("python_executor", {"code": "print(42)"})
        config = _trace_config(tmp, run_trace_run_id="fixed-run-id-abc")
        with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
            await dr.researcher_tools(state, config)
        path = os.path.join(tmp, "run_trace.jsonl")
        events = _read_jsonl(path)
        assert len(events) >= 2
        assert all(e["run_id"] == "fixed-run-id-abc" for e in events), "所有事件的run_id必须和调用方提供的一致"
        assert all(e["researcher_id"] == "" for e in events), (
            "裸调researcher_tools（没走supervisor_tools的分发）时researcher_id应该是空字符串，不是本模块自己发明的"
        )
        print("✅ test_provided_run_trace_run_id_is_consistent_across_all_events 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 10. 关闭追踪时，supervisor_tools传给researcher的config与原来是同一个对象
# ===========================================================================

async def test_disabled_tracing_preserves_config_object_identity():
    received_configs = []

    async def fake_ainvoke(payload, config=None):
        received_configs.append(config)
        return {"compressed_research": "x", "raw_notes": []}

    original_config = {"configurable": {}}  # 未启用run_trace
    tool_calls = [{"name": "ConductResearch", "args": {"research_topic": "T"}, "id": "c1"}]
    state = {
        "supervisor_messages": [AIMessage(content="", tool_calls=tool_calls)],
        "research_brief": "brief", "research_iterations": 1, "notes": [], "raw_notes": [],
    }
    with patch.object(dr.researcher_subgraph, "ainvoke", fake_ainvoke):
        await dr.supervisor_tools(state, original_config)

    assert len(received_configs) == 1
    assert received_configs[0] is original_config, (
        "关闭追踪时不应该构造任何config副本，必须是同一个对象（identity，不是内容相等）"
    )
    print("✅ test_disabled_tracing_preserves_config_object_identity 通过")


# ===========================================================================
# 11. 外层超时场景：trace中出现outer_timeout的tool_end，且不留未配对的tool_start
# ===========================================================================

async def test_outer_timeout_emits_paired_tool_end_with_no_unpaired_tool_start():
    tmp = tempfile.mkdtemp(prefix="trace_outer_timeout_")
    try:
        async def slow_call(code):
            await asyncio.sleep(10)
            return "too slow"

        state = _make_state("python_executor", {"code": "..."})
        config = _trace_config(tmp, tool_call_timeout_seconds=0.2, tool_retry_max_attempts=0)
        with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": slow_call}):
            await dr.researcher_tools(state, config)

        path = os.path.join(tmp, "run_trace.jsonl")
        events = _read_jsonl(path)
        outer_timeout_ends = [e for e in events if e["event_type"] == "tool_end" and e["status"] == "outer_timeout"]
        assert len(outer_timeout_ends) == 1, f"应该恰好有1条outer_timeout的tool_end，实际: {outer_timeout_ends}"

        result = summarize_trace.summarize(path)
        assert result["unpaired_tool_start_count"] == 0, (
            "这个场景下取消发生在第一次（也是唯一一次）attempt执行中途，补发的tool_end应该正好配上"
            f"那个tool_start，不应该有未配对的tool_start，实际: {result['unpaired_tool_start_count']}"
        )
        print("✅ test_outer_timeout_emits_paired_tool_end_with_no_unpaired_tool_start 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 12. 启用但未配置目录：零文件且仅有warning
# ===========================================================================

class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


async def test_enabled_without_dir_configured_warns_and_writes_nothing():
    run_trace._warned_missing_dir = False  # 重置，确保这次能观察到一条新warning

    handler = _CaptureHandler()
    logger = logging.getLogger("open_deep_research.run_trace")
    logger.addHandler(handler)
    try:
        config = {"configurable": {"run_trace_enabled": True}}  # 没配run_trace_dir

        makedirs_called = []
        with patch("os.makedirs", side_effect=lambda *a, **k: makedirs_called.append((a, k))):
            run_trace.emit("tool_start", config=config, research_topic="T", tool="x", attempt=1)

        assert makedirs_called == [], "没配目录时不应该尝试创建任何目录/文件"
        warnings = [r for r in handler.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"应该恰好有1条warning，实际: {len(warnings)}"
        assert "run_trace_dir" in warnings[0].getMessage()
        print("✅ test_enabled_without_dir_configured_warns_and_writes_nothing 通过")
    finally:
        logger.removeHandler(handler)


# ===========================================================================
# 13. 畸形config（审查后新增）：is_enabled()/emit()在任何畸形输入下都不应该
#     抛异常，也不应该尝试写任何文件。
# ===========================================================================

def test_malformed_config_never_raises_and_writes_nothing():
    malformed_configs = [
        {"configurable": None},                                            # configurable是None
        {"configurable": "not-a-dict"},                                    # configurable不是dict
        {"configurable": {"run_trace_enabled": True, "run_trace_dir": 12345}},  # run_trace_dir是整数
        None,                                                               # config本身是None
    ]
    with patch("os.makedirs", side_effect=AssertionError("不应该被调用——畸形config应该在走到这里之前就被挡住")):
        for bad_config in malformed_configs:
            result = run_trace.is_enabled(bad_config)  # 不应该抛异常
            assert result is False, f"畸形config应该被当作未启用，实际: {bad_config!r} -> {result}"
            # emit()本身也不应该抛异常（即使没有外层try包裹）
            run_trace.emit("tool_start", config=bad_config, research_topic="T", tool="x", attempt=1)
            run_trace.emit("tool_end", config=bad_config, research_topic="T", tool="x", attempt=1,
                            status="success", failure_kind="none", output="ok")
    print("✅ test_malformed_config_never_raises_and_writes_nothing 通过"
          f"（{len(malformed_configs)}种畸形config全部安全处理，zero次尝试创建目录）")


# ===========================================================================
# 14. "启用但未配目录"的warning在进程内只记一次，不是每次emit()调用都记
# ===========================================================================

def test_missing_dir_warning_logged_only_once_across_multiple_calls():
    run_trace._warned_missing_dir = False  # 重置

    handler = _CaptureHandler()
    logger = logging.getLogger("open_deep_research.run_trace")
    logger.addHandler(handler)
    try:
        config = {"configurable": {"run_trace_enabled": True}}  # 没配run_trace_dir
        for _ in range(3):
            run_trace.emit("tool_start", config=config, research_topic="T", tool="x", attempt=1)
        warnings = [r for r in handler.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"调用了3次emit()，但warning应该只记1条（进程内去重），实际: {len(warnings)}"
        print("✅ test_missing_dir_warning_logged_only_once_across_multiple_calls 通过（3次调用只产生1条warning）")
    finally:
        logger.removeHandler(handler)


async def main():
    test_event_format_has_all_required_fields()
    test_params_and_output_are_redacted()
    await test_concurrent_writes_produce_valid_non_interleaved_lines()
    await test_fail_open_unwritable_dir_does_not_change_main_flow_result()
    await test_disabled_tracing_creates_zero_files()
    await test_topic_c_real_graph_trace_reproduces_failure_sequence_with_stable_researcher_id()
    test_summarize_trace_on_fixed_fixture()
    test_summarize_trace_detects_unpaired_tool_end()
    test_summarize_trace_loop_detected_counts_and_reason_distribution()
    await test_parallel_researchers_get_distinct_researcher_ids_matching_their_topics()
    await test_provided_run_trace_run_id_is_consistent_across_all_events()
    await test_disabled_tracing_preserves_config_object_identity()
    await test_outer_timeout_emits_paired_tool_end_with_no_unpaired_tool_start()
    await test_enabled_without_dir_configured_warns_and_writes_nothing()
    test_malformed_config_never_raises_and_writes_nothing()
    test_missing_dir_warning_logged_only_once_across_multiple_calls()
    print("\n全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
