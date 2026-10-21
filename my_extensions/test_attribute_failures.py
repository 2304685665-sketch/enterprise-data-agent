"""attribute_failures 的离线测试：手写轨迹覆盖每个标签，并与 summarize_trace 的全局计数交叉核对。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribute_failures as af  # noqa: E402
import summarize_trace as st  # noqa: E402


class T:
    """按 seq 递增拼装事件。"""

    def __init__(self):
        self.n = 0
        self.ev = []

    def add(self, et, tool="sql_executor", rid="r1", call="c1", **kw):
        self.n += 1
        base = {"run_id": "x", "researcher_id": rid, "call_id": call, "seq": self.n, "event_type": et,
                "tool": tool, "attempt": None, "duration_ms": None, "status": None, "failure_kind": None, "reason": None}
        base.update(kw)
        self.ev.append(base)
        return self

    def write(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        for e in self.ev:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        f.close()
        return f.name


def attr(t):
    p = t.write()
    try:
        return af.attribute(p), st.summarize(p)
    finally:
        os.unlink(p)


def ok_call(t, call, tool="sql_executor", rid="r1"):
    t.add("tool_start", tool, rid, call, attempt=1)
    t.add("tool_end", tool, rid, call, attempt=1, duration_ms=5, status="success", failure_kind="none", output_preview="ok")


def test_each_label_is_assigned():
    t = T()
    ok_call(t, "c-ok")
    # 重试后成功
    t.add("tool_start", "internal_doc_search", "r1", "c-rec", attempt=1)
    t.add("tool_end", "internal_doc_search", "r1", "c-rec", attempt=1, status="error", failure_kind="infrastructure_error", output_preview="Document search error: boom")
    t.add("retry", "internal_doc_search", "r1", "c-rec", attempt=1, reason="transient_error")
    t.add("tool_start", "internal_doc_search", "r1", "c-rec", attempt=2)
    t.add("tool_end", "internal_doc_search", "r1", "c-rec", attempt=2, status="success", failure_kind="none", output_preview="ok")
    # 重试耗尽
    t.add("tool_start", "python_executor", "r1", "c-ex", attempt=1)
    t.add("tool_end", "python_executor", "r1", "c-ex", attempt=1, status="error", failure_kind="infrastructure_error", output_preview="x")
    t.add("retry", "python_executor", "r1", "c-ex", attempt=1, reason="transient_error")
    t.add("tool_start", "python_executor", "r1", "c-ex", attempt=2)
    t.add("tool_end", "python_executor", "r1", "c-ex", attempt=2, status="error", failure_kind="infrastructure_error", output_preview="x")
    # 基础设施错误未重试
    t.add("tool_start", "tavily_search", "r1", "c-un", attempt=1)
    t.add("tool_end", "tavily_search", "r1", "c-un", attempt=1, status="error", failure_kind="infrastructure_error", output_preview="Error executing tool: timeout")
    # 输入错误
    t.add("tool_start", "sql_executor", "r1", "c-in", attempt=1)
    t.add("tool_end", "sql_executor", "r1", "c-in", attempt=1, status="error", failure_kind="tool_input_error", output_preview="SQL Error: no such column")
    # 熔断拦截
    t.add("breaker_block", "sql_executor", "r1", "c-br", status="error", failure_kind="infrastructure_error", reason="circuit_breaker_open")
    # 无结果
    t.add("tool_start", "sql_executor", "r1", "c-no", attempt=1)
    a, s = attr(t)
    assert a["label_counts"] == {"ok": 1, "recovered_after_retry": 1, "retry_exhausted": 1, "infra_unretried": 1,
                                 "tool_input_error": 1, "blocked_by_breaker": 1, "no_result_recorded": 1}, a["label_counts"]
    assert a["total_invocations"] == 7
    by = {c["call_id"]: c for c in a["bad_cases"]}
    assert set(by) == {"c-ex", "c-un", "c-in", "c-br", "c-no"}
    assert by["c-in"]["subtype_heuristic"] == "sql_error" and by["c-ex"]["attempts"] == 2 and by["c-ex"]["retries"] == 1
    print("✅ test_each_label_is_assigned 通过")


def test_cross_check_with_summarize_trace():
    t = T()
    ok_call(t, "a")
    t.add("tool_start", "python_executor", "r1", "b", attempt=1)
    t.add("tool_end", "python_executor", "r1", "b", attempt=1, status="error", failure_kind="infrastructure_error")
    t.add("retry", "python_executor", "r1", "b")
    t.add("tool_start", "python_executor", "r1", "b", attempt=2)
    t.add("tool_end", "python_executor", "r1", "b", attempt=2, status="success", failure_kind="none")
    t.add("breaker_block", "sql_executor", "r1", "c", reason="circuit_breaker_open")
    t.add("tool_start", "sql_executor", "r1", "d", attempt=1)  # 悬挂
    a, s = attr(t)
    assert a["label_counts"].get("no_result_recorded", 0) == s["unpaired_tool_start_count"] == 1
    assert a["label_counts"].get("blocked_by_breaker", 0) == s["breaker_block_count"] == 1
    assert sum(v for k, v in a["label_counts"].items() if k == "recovered_after_retry") == 1 and s["retry_count"] == 1
    print("✅ test_cross_check_with_summarize_trace 通过")


def test_empty_call_id_sequential_calls_are_split():
    t = T()
    for _ in range(3):
        t.add("tool_start", "sql_executor", "r1", "", attempt=1)
        t.add("tool_end", "sql_executor", "r1", "", attempt=1, status="success", failure_kind="none")
    a, _ = attr(t)
    assert a["total_invocations"] == 3 and a["label_counts"] == {"ok": 3}, a
    print("✅ test_empty_call_id_sequential_calls_are_split 通过")


def test_researcher_level_events_and_by_tool():
    t = T()
    ok_call(t, "a")
    t.add("loop_detected", tool="sql_executor", reason="repeat")
    t.add("loop_detected", tool=None, reason="force_stop")
    t.add("degradation", tool="tavily_search", status="error", failure_kind="infrastructure_error", reason="x")
    a, _ = attr(t)
    assert a["loop_detected_by_reason"] == {"repeat": 1, "force_stop": 1}
    assert a["degradation_by_tool"] == {"tavily_search": 1} and a["by_tool"] == {"sql_executor": {"ok": 1}}
    print("✅ test_researcher_level_events_and_by_tool 通过")


def test_parallel_researchers_do_not_mix():
    t = T()
    t.add("tool_start", "sql_executor", "r1", "x", attempt=1)
    t.add("tool_start", "sql_executor", "r2", "x", attempt=1)
    t.add("tool_end", "sql_executor", "r2", "x", attempt=1, status="success", failure_kind="none")
    a, _ = attr(t)
    assert a["label_counts"] == {"ok": 1, "no_result_recorded": 1}, a
    assert [c["researcher_id"] for c in a["bad_cases"]] == ["r1"]
    print("✅ test_parallel_researchers_do_not_mix 通过")


def test_missing_file_content_and_blank_lines():
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n\n")
    f.close()
    try:
        a = af.attribute(f.name)
        assert a["total_invocations"] == 0 and a["bad_cases"] == []
    finally:
        os.unlink(f.name)
    print("✅ test_missing_file_content_and_blank_lines 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
