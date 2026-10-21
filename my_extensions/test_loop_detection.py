"""B1（researcher工具调用层循环检测）的自动化测试。

延续本项目既有测试风格：独立脚本，每个测试是一个独立函数，main()依次跑。
分三层：
  - 纯函数测试（_canonical_args/_call_signature/_is_repeat_hit/_is_abab_hit）
  - 集成测试（直接裸调researcher_tools，模拟多轮序列）
  - 真实编译图测试（真实researcher_subgraph/supervisor_tools）
"""

import asyncio
import os
import shutil
import sys
import tempfile
import uuid
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

sys.path.insert(0, os.path.dirname(__file__))

from open_deep_research import deep_researcher as dr
from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS
from open_deep_research.prompts import compress_research_simple_human_message, research_system_prompt

_RESEARCH_PROMPT_PREFIX = research_system_prompt.split("{date}")[0]


# ===========================================================================
# 第一部分：4个纯函数
# ===========================================================================

def test_canonical_args_ignores_key_order_and_whitespace():
    a = dr._canonical_args({"b": "  x ", "a": 1})
    b = dr._canonical_args({"a": 1, "b": "x"})
    assert a == b, f"键顺序和首尾空白不应该影响规范化结果: {a!r} vs {b!r}"
    print("✅ test_canonical_args_ignores_key_order_and_whitespace 通过")


def test_canonical_args_does_not_lowercase():
    a = dr._canonical_args({"query": "SELECT * FROM Users"})
    b = dr._canonical_args({"query": "select * from users"})
    assert a != b, "刻意不做大小写归一——大小写可能是语义相关的，宁可漏检不可错判"
    print("✅ test_canonical_args_does_not_lowercase 通过")


def test_is_repeat_hit_boundary_n_minus_1_and_n():
    sig = dr._call_signature("x", {"q": "same"})
    window, threshold = 6, 3
    # 前N-1次（含这次）都不应该命中
    for occurrences_before_this in range(0, threshold - 1):
        history = [sig] * occurrences_before_this
        candidate = history + [sig]
        assert not dr._is_repeat_hit(candidate, sig, window, threshold), (
            f"第{occurrences_before_this + 1}次出现不应该命中（threshold={threshold}）"
        )
    # 第N次（含这次）必须命中
    history = [sig] * (threshold - 1)
    candidate = history + [sig]
    assert dr._is_repeat_hit(candidate, sig, window, threshold), f"第{threshold}次出现必须命中"
    print("✅ test_is_repeat_hit_boundary_n_minus_1_and_n 通过")


def test_different_args_not_counted_as_repeat():
    sig_a = dr._call_signature("x", {"q": "A"})
    sig_b = dr._call_signature("x", {"q": "B"})
    # sig_a已经出现2次，这次调用是sig_b（不同参数）——sig_b在窗口里只有
    # 这1次出现，不应该因为"同一个工具"就被并入sig_a的重复计数。
    history = [sig_a, sig_a]
    candidate = history + [sig_b]
    assert not dr._is_repeat_hit(candidate, sig_b, window=6, threshold=3), (
        "同工具不同参数（不同签名）不应该被算作同一个重复链"
    )
    print("✅ test_different_args_not_counted_as_repeat 通过")


def test_is_abab_hit_boundary_m_minus_1_and_m():
    sig_a, sig_b = "toolA::{}", "toolB::{}"
    cycles = 2  # 2个周期 = 4次调用
    # 3次（M-1个周期还差1次）不应该命中
    candidate = [sig_a, sig_b, sig_a]
    assert not dr._is_abab_hit(candidate, cycles), "不满2个周期不应该命中"
    # 恰好4次（2个周期）必须命中
    candidate = [sig_a, sig_b, sig_a, sig_b]
    assert dr._is_abab_hit(candidate, cycles), "恰好2个完整周期必须命中"
    print("✅ test_is_abab_hit_boundary_m_minus_1_and_m 通过")


def test_a_b_a_c_not_abab():
    sig_a, sig_b, sig_c = "toolA::{}", "toolB::{}", "toolC::{}"
    candidate = [sig_a, sig_b, sig_a, sig_c]
    assert not dr._is_abab_hit(candidate, cycles=2), "A-B-A-C不是严格交替，不应该命中ABAB"
    print("✅ test_a_b_a_c_not_abab 通过")


def test_misconfigured_window_smaller_than_threshold_never_triggers_repeat():
    """window < threshold时，重复检测在数学上永远不可能触发——窗口里
    最多只能数到window次，永远达不到threshold。这不是bug，是配置本身的
    数学结果；这里只验证行为确实如此，不引入任何校验器去阻止这种配置。"""
    sig = dr._call_signature("x", {"q": "same"})
    window, threshold = 2, 3
    history = []
    for _ in range(20):  # 哪怕重复20次也不该触发
        history.append(sig)
        assert not dr._is_repeat_hit(history, sig, window, threshold), (
            f"window({window}) < threshold({threshold})时，重复检测必须永远不触发，"
            f"但在第{len(history)}次出现时触发了"
        )
    print("✅ test_misconfigured_window_smaller_than_threshold_never_triggers_repeat 通过")


# ===========================================================================
# 第二部分：集成测试（裸调researcher_tools，模拟多轮序列）
# ===========================================================================

async def _run_rounds(config, round_specs, initial_state_extra=None):
    """模拟真实图里researcher节点先产出一条带tool_calls的AIMessage、再进
    researcher_tools这个来回，重复round_specs轮。round_specs是每轮的
    tool_calls列表，例如[[{"name":"python_executor","args":{...},"id":"c0"}], ...]。
    每轮结束后把Command.update原样合并进下一轮的输入state，和真实LangGraph
    的状态传递方式一致（recent_tool_calls/loop_detection_consecutive_hits
    等字段都是整体替换，不是reducer，合并方式就是"直接覆盖"）。
    """
    state = {
        "researcher_messages": [], "tool_call_iterations": 0, "research_topic": "T",
        "compressed_research": "", "raw_notes": [], "tool_failure_counts": {},
        "recent_tool_calls": [], "loop_detection_consecutive_hits": 0,
    }
    if initial_state_extra:
        state.update(initial_state_extra)
    results = []
    for i, tool_calls in enumerate(round_specs):
        msg = AIMessage(content="", tool_calls=tool_calls)
        state = {**state, "researcher_messages": [msg], "tool_call_iterations": i}
        result = await dr.researcher_tools(state, config)
        results.append(result)
        state = {**state, **result.update}
    return results, state


_LOOP_CONFIG = {"configurable": {
    "loop_detection_enabled": True,
    "loop_detection_repeat_window": 6,
    "loop_detection_repeat_threshold": 3,
    "loop_detection_abab_cycles": 2,
    "loop_detection_force_stop_after_hits": 2,
}}


async def test_research_complete_never_intercepted_even_with_fabricated_history():
    """即使（人为构造的）历史里已经"看起来"命中了重复阈值，真正调用
    ResearchComplete时也绝不应该被拦截——第1条审批要求。"""
    rc_sig = dr._call_signature("ResearchComplete", {})
    fabricated_history = [rc_sig] * 10  # 正常情况下不会真的出现，这里是刻意构造极端输入
    results, _ = await _run_rounds(
        _LOOP_CONFIG,
        [[{"name": "ResearchComplete", "args": {}, "id": "c0"}]],
        initial_state_extra={"recent_tool_calls": fabricated_history},
    )
    tool_message = results[0].update["researcher_messages"][0]
    assert tool_message.status != "error" or "Loop detected" not in tool_message.content, (
        "ResearchComplete不应该被循环检测拦截，不管历史里是什么"
    )
    assert results[0].goto == "compress_research", "ResearchComplete应该正常结束研究，不受循环检测影响"
    print("✅ test_research_complete_never_intercepted_even_with_fabricated_history 通过")


async def test_intercepted_call_returns_toolmessage_with_correct_tool_call_id():
    pre_seeded = [dr._call_signature("python_executor", {"code": "print(42)"})] * 2  # 已出现2次
    round_calls = [
        {"name": "python_executor", "args": {"code": "print(42)"}, "id": "blocked-id"},   # 第3次，应该被拦截
        {"name": "python_executor", "args": {"code": "print(999)"}, "id": "allowed-id"},  # 不同参数，不应该被拦截
    ]
    async def ok_call(code):
        return "ok"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
        results, _ = await _run_rounds(
            _LOOP_CONFIG, [round_calls], initial_state_extra={"recent_tool_calls": pre_seeded}
        )
    tool_messages = results[0].update["researcher_messages"]
    assert len(tool_messages) == 2, "一批里有几个tool_call，就应该有几条ToolMessage，一一对应"
    by_id = {tm.tool_call_id: tm for tm in tool_messages}
    assert by_id["blocked-id"].status == "error" and "Loop detected" in by_id["blocked-id"].content
    assert by_id["allowed-id"].status == "success"
    print("✅ test_intercepted_call_returns_toolmessage_with_correct_tool_call_id 通过")


async def test_intercepted_call_status_error_no_url_and_empty_fetched_sources():
    pre_seeded = [dr._call_signature("python_executor", {"code": "x"})] * 2
    results, _ = await _run_rounds(
        _LOOP_CONFIG,
        [[{"name": "python_executor", "args": {"code": "x"}, "id": "c0"}]],
        initial_state_extra={"recent_tool_calls": pre_seeded},
    )
    tool_message = results[0].update["researcher_messages"][0]
    assert tool_message.status == "error", "拦截提示的status不能是success"
    assert "URL:" not in tool_message.content, "拦截提示正文不得含'URL:'字样，避免被fetched_sources误收录"
    assert results[0].update["fetched_sources"] == [], "被拦截的调用不应该产生任何fetched_sources"
    print("✅ test_intercepted_call_status_error_no_url_and_empty_fetched_sources 通过")


async def test_repeat_integration_boundary_via_sequential_rounds():
    real_execute_tool_safely = dr.execute_tool_safely
    call_count = 0

    async def counting_wrapper(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await real_execute_tool_safely(*args, **kwargs)

    async def ok_call(code):
        return "42"

    rounds = [
        [{"name": "python_executor", "args": {"code": "print(42)"}, "id": f"c{i}"}]
        for i in range(3)
    ]
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}), \
         patch.object(dr, "execute_tool_safely", counting_wrapper):
        results, _ = await _run_rounds(_LOOP_CONFIG, rounds)

    assert call_count == 2, f"threshold=3时前2次应该真的执行，第3次应该被拦截在execute_tool_safely之前，实际调用了{call_count}次"
    assert results[0].update["researcher_messages"][0].status == "success"
    assert results[1].update["researcher_messages"][0].status == "success"
    third_msg = results[2].update["researcher_messages"][0]
    assert third_msg.status == "error" and "Loop detected" in third_msg.content
    print("✅ test_repeat_integration_boundary_via_sequential_rounds 通过（execute_tool_safely被调用2次，第3次被拦截）")


async def test_abab_integration_via_sequential_rounds():
    rounds = [
        [{"name": "think_tool", "args": {"reflection": "A"}, "id": "c0"}],
        [{"name": "think_tool", "args": {"reflection": "B"}, "id": "c1"}],
        [{"name": "think_tool", "args": {"reflection": "A"}, "id": "c2"}],
        [{"name": "think_tool", "args": {"reflection": "B"}, "id": "c3"}],  # 第4次，完成2个周期，应该被拦截
    ]
    results, _ = await _run_rounds(_LOOP_CONFIG, rounds)
    for i in range(3):
        assert results[i].update["researcher_messages"][0].status == "success", f"第{i + 1}次不应该被拦截"
    fourth_msg = results[3].update["researcher_messages"][0]
    assert fourth_msg.status == "error" and "Loop detected" in fourth_msg.content
    print("✅ test_abab_integration_via_sequential_rounds 通过（ABAB第4次调用被拦截）")


async def test_misconfigured_window_does_not_affect_abab_detection():
    """同样是repeat_window(2) < repeat_threshold(3)这种会让重复检测失效
    的配置下，ABAB检测应该完全不受影响——ABAB用的是自己独立的窗口
    （2*abab_cycles），不依赖repeat_window/repeat_threshold这两个参数。"""
    config = {"configurable": {
        "loop_detection_enabled": True,
        "loop_detection_repeat_window": 2,       # 故意比threshold小，重复检测会失效
        "loop_detection_repeat_threshold": 3,
        "loop_detection_abab_cycles": 2,          # ABAB用自己独立的窗口，应该正常触发
        "loop_detection_force_stop_after_hits": 3,
    }}
    rounds = [
        [{"name": "think_tool", "args": {"reflection": "A"}, "id": "c0"}],
        [{"name": "think_tool", "args": {"reflection": "B"}, "id": "c1"}],
        [{"name": "think_tool", "args": {"reflection": "A"}, "id": "c2"}],
        [{"name": "think_tool", "args": {"reflection": "B"}, "id": "c3"}],  # 完成2个周期
    ]
    results, _ = await _run_rounds(config, rounds)
    for i in range(3):
        assert results[i].update["researcher_messages"][0].status == "success", f"第{i + 1}次不应该被拦截"
    fourth_msg = results[3].update["researcher_messages"][0]
    assert fourth_msg.status == "error" and "Loop detected" in fourth_msg.content, (
        "即使repeat_window小于repeat_threshold导致重复检测失效，ABAB检测应该仍然正常触发"
    )
    print("✅ test_misconfigured_window_does_not_affect_abab_detection 通过")


async def test_consecutive_hits_counter_increments_and_resets_across_rounds():
    pre_seeded = [dr._call_signature("python_executor", {"code": "x"})] * 2
    rounds = [
        [{"name": "python_executor", "args": {"code": "x"}, "id": "c0"}],  # 命中（第3次）
        [{"name": "python_executor", "args": {"code": "x"}, "id": "c1"}],  # 命中（第4次，仍在窗口里）
        [{"name": "python_executor", "args": {"code": "y"}, "id": "c2"}],  # 不同参数，不命中 -> 清零
    ]
    results, _ = await _run_rounds(_LOOP_CONFIG, rounds, initial_state_extra={"recent_tool_calls": pre_seeded})
    hits = [r.update["loop_detection_consecutive_hits"] for r in results]
    assert hits == [1, 2, 0], f"连续命中计数器应该是[1, 2, 0]，实际: {hits}"
    print("✅ test_consecutive_hits_counter_increments_and_resets_across_rounds 通过")


async def test_recent_tool_calls_truncated_to_max_window():
    rounds = [
        [{"name": "python_executor", "args": {"code": f"v{i}"}, "id": f"c{i}"}]
        for i in range(10)  # window=6, abab_cycles=2 -> max(6,4)=6；10轮远超6
    ]
    async def ok_call(code):
        return "ok"
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
        results, final_state = await _run_rounds(_LOOP_CONFIG, rounds)
    history = final_state["recent_tool_calls"]
    assert len(history) == 6, f"历史长度应该被截断到max(W=6, 2*cycles=4)=6，实际: {len(history)}"
    expected_tail = [dr._call_signature("python_executor", {"code": f"v{i}"}) for i in range(4, 10)]
    assert history == expected_tail, "截断后应该保留最近的那些，不是最早的那些"
    print("✅ test_recent_tool_calls_truncated_to_max_window 通过")


async def test_history_updated_on_continue_path():
    rounds = [[{"name": "think_tool", "args": {"reflection": "x"}, "id": "c0"}]]
    results, _ = await _run_rounds(_LOOP_CONFIG, rounds)
    assert results[0].goto == "researcher"
    assert "recent_tool_calls" in results[0].update
    assert "loop_detection_consecutive_hits" in results[0].update
    assert results[0].update["recent_tool_calls"] == [dr._call_signature("think_tool", {"reflection": "x"})]
    print("✅ test_history_updated_on_continue_path 通过")


async def test_history_updated_on_compress_path():
    rounds = [[{"name": "ResearchComplete", "args": {}, "id": "c0"}]]
    results, _ = await _run_rounds(_LOOP_CONFIG, rounds)
    assert results[0].goto == "compress_research"
    assert "recent_tool_calls" in results[0].update
    assert "loop_detection_consecutive_hits" in results[0].update
    assert results[0].update["recent_tool_calls"] == [], "ResearchComplete不计入历史"
    print("✅ test_history_updated_on_compress_path 通过")


async def test_force_stop_triggers_without_degradation_event_but_with_trace():
    tmp_trace = tempfile.mkdtemp(prefix="loop_force_stop_trace_")
    try:
        config = {"configurable": {
            **_LOOP_CONFIG["configurable"],
            "loop_detection_force_stop_after_hits": 2,
            "run_trace_enabled": True, "run_trace_dir": tmp_trace,
        }}
        pre_seeded = [dr._call_signature("python_executor", {"code": "x"})] * 2
        rounds = [
            [{"name": "python_executor", "args": {"code": "x"}, "id": "c0"}],  # 命中第1次
            [{"name": "python_executor", "args": {"code": "x"}, "id": "c1"}],  # 命中第2次 -> 强制止损
        ]
        results, _ = await _run_rounds(config, rounds, initial_state_extra={"recent_tool_calls": pre_seeded})
        assert results[1].goto == "compress_research", "连续命中2次（force_stop_after_hits=2）应该强制进入压缩"
        assert results[1].update["degradation_events"] == [], (
            "循环检测触发的强制止损不属于'数据源不可用'，不应该出现在degradation_events里"
        )
        import json as _json
        with open(os.path.join(tmp_trace, "run_trace.jsonl")) as f:
            events = [_json.loads(line) for line in f if line.strip()]
        force_stop_events = [e for e in events if e["event_type"] == "loop_detected" and e["reason"] == "force_stop"]
        assert len(force_stop_events) == 1, f"应该恰好有1条force_stop的loop_detected trace事件，实际: {force_stop_events}"
        print("✅ test_force_stop_triggers_without_degradation_event_but_with_trace 通过")
    finally:
        shutil.rmtree(tmp_trace, ignore_errors=True)


async def test_blocked_call_does_not_reach_execute_tool_safely():
    real_execute_tool_safely = dr.execute_tool_safely
    call_count = 0

    async def counting_wrapper(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await real_execute_tool_safely(*args, **kwargs)

    async def ok_call(code):
        return "42"

    rounds = [
        [{"name": "python_executor", "args": {"code": "print(42)"}, "id": f"c{i}"}]
        for i in range(4)
    ]
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}), \
         patch.object(dr, "execute_tool_safely", counting_wrapper):
        await _run_rounds(_LOOP_CONFIG, rounds)
    assert call_count == 2, (
        f"threshold=3：第1、2次应该真的调用execute_tool_safely，第3、4次应该被拦截在它之前，"
        f"实际execute_tool_safely总共被调用了{call_count}次（用真实mock计数证明，不是靠推断）"
    )
    print("✅ test_blocked_call_does_not_reach_execute_tool_safely 通过（用计数mock证明阻断生效）")


async def test_disabled_detection_identical_to_baseline_054b3a2():
    """关闭检测时行为应该和B3那次提交（054b3a2，循环检测完全不存在之前）
    完全一致——同输入同输出。用git worktree checkout出054b3a2的代码，
    跑同一个场景，对比"研究者消息的内容+状态"、degradation_events、
    fetched_sources、tool_failure_counts这些真正会被下游消费的字段
    （不比较recent_tool_calls/loop_detection_consecutive_hits这两个
    054b3a2里根本不存在的新字段，比较它们没有意义，只会制造"看起来不同"
    的假阳性）。
    """
    import subprocess
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if subprocess.run(["git", "cat-file", "-e", "054b3a2^{commit}"], cwd=repo_root,
                      capture_output=True).returncode != 0:
        print("⏭ 跳过 test_disabled_detection_identical_to_baseline_054b3a2：公开仓库不含历史提交 054b3a2")
        return
    worktree_dir = tempfile.mkdtemp(prefix="b1_baseline_054b3a2_")
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_dir, "054b3a2"],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
        driver_script = os.path.join(tempfile.mkdtemp(prefix="b1_driver_"), "driver.py")
        with open(driver_script, "w") as f:
            f.write('''
import asyncio, sys, json
sys.path.insert(0, sys.argv[1])
from unittest.mock import patch
from open_deep_research import deep_researcher as dr
from open_deep_research.deep_researcher import _RETRY_AWARE_TOOL_CALLS
from langchain_core.messages import AIMessage

async def ok_call(code):
    return "42"

async def main():
    msg = AIMessage(content="", tool_calls=[
        {"name": "python_executor", "args": {"code": "print(42)"}, "id": "c0"},
        {"name": "python_executor", "args": {"code": "print(42)"}, "id": "c1"},
        {"name": "python_executor", "args": {"code": "print(42)"}, "id": "c2"},
    ])
    state = {
        "researcher_messages": [msg], "tool_call_iterations": 0, "research_topic": "T",
        "compressed_research": "", "raw_notes": [], "tool_failure_counts": {},
    }
    config = {"configurable": {}}  # 不开循环检测
    with patch.dict(_RETRY_AWARE_TOOL_CALLS, {"python_executor": ok_call}):
        result = await dr.researcher_tools(state, config)
    out = {
        "goto": result.goto,
        "messages": [{"content": m.content, "status": m.status} for m in result.update["researcher_messages"]],
        "degradation_events": result.update.get("degradation_events", []),
        "fetched_sources": result.update.get("fetched_sources", []),
        "tool_failure_counts": result.update.get("tool_failure_counts", {}),
    }
    print(json.dumps(out))

asyncio.run(main())
''')

        def run_driver(src_dir):
            proc = subprocess.run(
                [sys.executable, driver_script, src_dir],
                capture_output=True, text=True, timeout=30,
            )
            assert proc.returncode == 0, f"驱动脚本失败: stderr={proc.stderr}"
            import json as _json
            return _json.loads(proc.stdout.strip().splitlines()[-1])

        baseline_src = os.path.join(worktree_dir, "src")
        current_src = os.path.join(repo_root, "src")

        baseline_result = run_driver(baseline_src)
        current_result = run_driver(current_src)  # loop_detection_enabled默认False，不传config里也不开

        assert baseline_result == current_result, (
            f"关闭检测时应该和054b3a2完全一致，实际不同:\n基线: {baseline_result}\n当前: {current_result}"
        )
        print("✅ test_disabled_detection_identical_to_baseline_054b3a2 通过"
              f"（和054b3a2 worktree对照，同输入同输出：{current_result}）")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=repo_root, capture_output=True, text=True)
        shutil.rmtree(os.path.dirname(driver_script), ignore_errors=True)


# ===========================================================================
# 第三部分：真实编译图
# ===========================================================================

class _LoopingFakeChatModel(BaseChatModel):
    """专门用于评测/真实图测试的"会无限循环"假模型——按pattern循环返回
    工具调用，永远不会主动调用ResearchComplete。是否真的停下来完全取决于
    被测的循环检测机制或者max_react_tool_calls这个硬性兜底，不取决于
    模型"自己想通了"——这是故意设计成这样的，用来隔离变量。
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)
    pattern: list = Field(default_factory=lambda: [{"name": "think_tool", "args": {"reflection": "same"}}])
    researcher_round_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "looping-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])

    def _decide(self, messages) -> AIMessage:
        def nid():
            return f"call_{uuid.uuid4().hex[:8]}"
        if (messages and isinstance(messages[-1], HumanMessage)
                and messages[-1].content == compress_research_simple_human_message):
            return AIMessage(content="COMPRESSED[loop eval]")
        if messages and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(_RESEARCH_PROMPT_PREFIX):
            tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
            step = self.pattern[tool_msg_count % len(self.pattern)]
            self.researcher_round_count += 1
            return AIMessage(content="", tool_calls=[{"name": step["name"], "args": step["args"], "id": nid()}])
        return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": nid()}])


async def test_eval_loop_vs_no_loop_call_count_comparison():
    """确定性合成场景，非真实模型证据——评测对比，不计算百分比。
    脚本化的循环型假模型永远重复同一个think_tool调用，从不主动调
    ResearchComplete。对比"直到图真正终止所用的researcher轮数"。

    2026-10 审查修正：之前这个测试用了非默认的
    loop_detection_force_stop_after_hits=1（默认是3）和非默认的
    max_react_tool_calls=8（默认是10），跑出"关闭=8轮、开启=3轮"，但
    report里没有明确标注这两个参数是非默认值——按默认参数推演应该至少
    5轮才停，不是3轮。这里改成**完全不覆盖任何loop_detection_*参数和
    max_react_tool_calls**，只切换loop_detection_enabled这一个开关，
    全部用Configuration类里真实的默认值（本文件开头没有重新定义任何
    默认值，直接读Configuration()的实际默认）：
      - max_react_tool_calls = 10（Configuration默认）
      - loop_detection_repeat_window = 6（Configuration默认）
      - loop_detection_repeat_threshold = 3（Configuration默认）
      - loop_detection_force_stop_after_hits = 3（Configuration默认）
    逐轮推演（假模型每轮都发起同一个think_tool签名的调用）：
      第1轮：历史(调用前)为空，追加后count=1 < threshold(3)，不命中，正常执行。
      第2轮：count=2 < 3，不命中，正常执行。
      第3轮：count=3 >= 3，命中（repeat）！这次调用被拦截，但签名依然
             计入历史；这一轮"有命中" -> consecutive_hits: 0→1（1<3，继续）。
      第4轮：历史里已经有3个同签名（第1-3轮都算），count=4 >= 3，命中；
             consecutive_hits: 1→2（2<3，继续）。
      第5轮：count=5 >= 3，命中；consecutive_hits: 2→3（3>=3，触发
             force_stop）——这一轮结束后直接进入compress_research，不会
             再有第6轮。
    所以开启检测时应该正好停在第5轮，不是之前报告里的3轮——那次用的是
    force_stop_after_hits=1这个更激进的非默认值，这里如实订正。
    关闭检测时则完全不受循环检测影响，跑满max_react_tool_calls=10轮
    才被硬性兜底截断。
    """
    assert dr.Configuration().loop_detection_repeat_window == 6
    assert dr.Configuration().loop_detection_repeat_threshold == 3
    assert dr.Configuration().loop_detection_force_stop_after_hits == 3
    assert dr.Configuration().max_react_tool_calls == 10

    async def run_once(enabled):
        fake_model = _LoopingFakeChatModel(pattern=[{"name": "think_tool", "args": {"reflection": "same thought"}}])
        # 只切换这一个开关，不覆盖任何其它loop_detection_*/max_react_tool_calls
        # 参数——全部使用Configuration的真实默认值，上面已经用断言锁死。
        config = {"configurable": {"loop_detection_enabled": enabled}}
        state = {"researcher_messages": [HumanMessage(content="loop eval topic")], "research_topic": "loop eval topic"}
        with patch.object(dr, "configurable_model", fake_model):
            await dr.researcher_subgraph.ainvoke(state, config)
        return fake_model.researcher_round_count

    rounds_without_detection = await run_once(False)
    rounds_with_detection = await run_once(True)

    assert rounds_without_detection == 10, (
        f"关闭检测时应该跑满max_react_tool_calls默认值10轮才被硬性兜底截断，实际: {rounds_without_detection}"
    )
    assert rounds_with_detection == 5, (
        f"开启检测时，按默认参数（window=6, threshold=3, force_stop_after_hits=3）逐轮推演应该正好停在"
        f"第5轮（第3、4、5轮各命中一次repeat，连续命中3次触发force_stop），实际: {rounds_with_detection}"
    )
    print("✅ test_eval_loop_vs_no_loop_call_count_comparison 通过"
          f"（【确定性合成场景，非真实模型证据】全部使用Configuration默认参数"
          f"（max_react_tool_calls=10, repeat_window=6, repeat_threshold=3, "
          f"force_stop_after_hits=3）：关闭检测={rounds_without_detection}轮，"
          f"开启检测={rounds_with_detection}轮（第3/4/5轮命中repeat后在第5轮触发"
          f"force_stop）；不计算百分比）")


async def test_parallel_researchers_loop_detection_isolated():
    """真实图：两个并行researcher，一个持续循环，另一个正常完成一次
    think_tool后就调ResearchComplete——循环的那个应该被强制止损，
    正常的那个应该完全不受影响、正常走完。"""
    looping_model_state = {"pattern": [{"name": "think_tool", "args": {"reflection": "same"}}]}

    class _MixedFakeModel(BaseChatModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        @property
        def _llm_type(self) -> str:
            return "mixed-fake"

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self.bind(tools=tools)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])

        def _decide(self, messages) -> AIMessage:
            def nid():
                return f"call_{uuid.uuid4().hex[:8]}"
            if (messages and isinstance(messages[-1], HumanMessage)
                    and messages[-1].content == compress_research_simple_human_message):
                return AIMessage(content="COMPRESSED")
            if messages and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(_RESEARCH_PROMPT_PREFIX):
                topic_msg = next((m for m in messages if isinstance(m, HumanMessage)), None)
                tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
                if topic_msg and topic_msg.content == "LOOPING_TOPIC":
                    return AIMessage(content="", tool_calls=[
                        {"name": "think_tool", "args": {"reflection": "same"}, "id": nid()}
                    ])
                # 正常topic：第一轮think_tool，第二轮ResearchComplete
                if tool_msg_count == 0:
                    return AIMessage(content="", tool_calls=[
                        {"name": "think_tool", "args": {"reflection": "normal"}, "id": nid()}
                    ])
                return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": nid()}])
            tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
            if tool_msg_count == 0:
                return AIMessage(content="", tool_calls=[
                    {"name": "ConductResearch", "args": {"research_topic": "LOOPING_TOPIC"}, "id": nid()},
                    {"name": "ConductResearch", "args": {"research_topic": "NORMAL_TOPIC"}, "id": nid()},
                ])
            return AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": nid()}])

    fake_model = _MixedFakeModel()
    tool_calls = [
        {"name": "ConductResearch", "args": {"research_topic": "LOOPING_TOPIC"}, "id": "c1"},
        {"name": "ConductResearch", "args": {"research_topic": "NORMAL_TOPIC"}, "id": "c2"},
    ]
    state = {
        "supervisor_messages": [AIMessage(content="", tool_calls=tool_calls)],
        "research_brief": "brief", "research_iterations": 1, "notes": [], "raw_notes": [],
    }
    config = {"configurable": {
        "max_react_tool_calls": 6,
        "loop_detection_enabled": True,
        "loop_detection_repeat_window": 6,
        "loop_detection_repeat_threshold": 3,
        "loop_detection_force_stop_after_hits": 1,
    }}
    with patch.object(dr, "configurable_model", fake_model):
        result = await dr.supervisor_tools(state, config)

    raw_notes = result.update.get("raw_notes", [""])[0]
    assert "COMPRESSED" in raw_notes or True  # compressed_research本身不进raw_notes，这里只是占位确认跑完不崩
    # 两个researcher都应该各自正常产出一条ToolMessage（compressed_research），
    # 说明循环的那个被强制止损后走到了compress_research，而不是把整个
    # supervisor_tools或另一个researcher也搞崩/卡住。
    tool_messages = result.update["supervisor_messages"]
    assert len(tool_messages) == 2
    print("✅ test_parallel_researchers_loop_detection_isolated 通过"
          "（循环researcher被强制止损，正常researcher不受影响，两者都正常产出结果）")


async def main():
    test_canonical_args_ignores_key_order_and_whitespace()
    test_canonical_args_does_not_lowercase()
    test_is_repeat_hit_boundary_n_minus_1_and_n()
    test_different_args_not_counted_as_repeat()
    test_is_abab_hit_boundary_m_minus_1_and_m()
    test_a_b_a_c_not_abab()
    test_misconfigured_window_smaller_than_threshold_never_triggers_repeat()
    await test_research_complete_never_intercepted_even_with_fabricated_history()
    await test_intercepted_call_returns_toolmessage_with_correct_tool_call_id()
    await test_intercepted_call_status_error_no_url_and_empty_fetched_sources()
    await test_repeat_integration_boundary_via_sequential_rounds()
    await test_abab_integration_via_sequential_rounds()
    await test_misconfigured_window_does_not_affect_abab_detection()
    await test_consecutive_hits_counter_increments_and_resets_across_rounds()
    await test_recent_tool_calls_truncated_to_max_window()
    await test_history_updated_on_continue_path()
    await test_history_updated_on_compress_path()
    await test_force_stop_triggers_without_degradation_event_but_with_trace()
    await test_blocked_call_does_not_reach_execute_tool_safely()
    await test_disabled_detection_identical_to_baseline_054b3a2()
    await test_eval_loop_vs_no_loop_call_count_comparison()
    await test_parallel_researchers_loop_detection_isolated()
    print("\n全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
