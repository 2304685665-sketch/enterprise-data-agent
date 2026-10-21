"""attribute_failures.py —— 把 run_trace 的 JSONL 轨迹按"工具调用"逐次归因，输出 Bad Case 清单。

和 summarize_trace.py 的区别：summarize_trace 给的是全局计数；本脚本把事件还原成一次次工具调用
（invocation），给每次调用一个确定性的归因标签并附证据（事件 seq、尝试次数、输出预览），
再按标签汇总，方便"从统计下钻到具体哪几次调用出了问题"。

标签（按规则判定，无模型参与，规则见 classify_invocation）：
  ok                    成功且只尝试了一次
  recovered_after_retry 重试后成功（attempts > 1，最终成功）
  retry_exhausted       发生过重试但最终仍是基础设施错误
  infra_unretried       基础设施错误，且这次调用没有任何重试（如被标为"不可重试"的超时）
  tool_input_error      工具正常返回了错误信息（SQL/代码/参数问题），非基础设施故障
  blocked_by_breaker    被熔断直接拦截，没有真正执行
  no_result_recorded    有 tool_start 但始终没有 tool_end（崩溃、外层超时未补发等）
另外单独汇总 researcher 级事件：loop_detected（按 reason）与 degradation。

已知边界：
- tool_input_error 的细分（sql_error / python_error / other）只看 output_preview 的既有错误前缀，
  是展示性的启发式，不影响上面的主标签。
- call_id 缺失时按 (researcher_id, tool) 顺序切分调用，只保证数量对得上，不保证逐次对应。
- 本脚本只读轨迹文件，不修改任何数据。

用法：python attribute_failures.py <run_trace.jsonl> [--json]
"""

import argparse
import json
from collections import defaultdict

_SUBTYPE_PREFIXES = (("SQL Error:", "sql_error"), ("Python Error:", "python_error"), ("Document search error:", "doc_search_error"))


def load_events(path: str) -> list[dict]:
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return sorted(events, key=lambda e: e.get("seq", 0))


def _key(e):
    return (e.get("researcher_id"), e.get("tool"), e.get("call_id") or "")


def build_invocations(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (invocations, researcher_level_events)。"""
    invs: list[dict] = []
    current: dict[tuple, dict] = {}
    researcher_events: list[dict] = []
    for e in events:
        et = e.get("event_type")
        k = _key(e)
        if et == "tool_start":
            cur = current.get(k)
            continuing = cur is not None and (cur["pending_retry"] or cur["ends"] == 0)
            if not continuing:
                cur = {"key": k, "starts": 0, "ends": 0, "retries": 0, "pending_retry": False,
                       "last_end": None, "blocked": False, "seqs": []}
                invs.append(cur)
                current[k] = cur
            cur["starts"] += 1
            cur["pending_retry"] = False
            cur["seqs"].append(e.get("seq"))
        elif et == "tool_end":
            cur = current.get(k)
            if cur is None:  # 没有对应 start 的 end：单独成一次调用，标记为缺 start
                cur = {"key": k, "starts": 0, "ends": 0, "retries": 0, "pending_retry": False,
                       "last_end": None, "blocked": False, "seqs": []}
                invs.append(cur)
                current[k] = cur
            cur["ends"] += 1
            cur["last_end"] = e
            cur["seqs"].append(e.get("seq"))
        elif et == "retry":
            cur = current.get(k)
            if cur is not None:
                cur["retries"] += 1
                cur["pending_retry"] = True
                cur["seqs"].append(e.get("seq"))
        elif et == "breaker_block":
            invs.append({"key": k, "starts": 0, "ends": 0, "retries": 0, "pending_retry": False,
                         "last_end": None, "blocked": True, "seqs": [e.get("seq")]})
        elif et in ("loop_detected", "degradation"):
            researcher_events.append(e)
    return invs, researcher_events


def classify_invocation(inv: dict) -> str:
    if inv["blocked"]:
        return "blocked_by_breaker"
    end = inv["last_end"]
    if end is None:
        return "no_result_recorded"
    fk = end.get("failure_kind")
    attempts = max(inv["starts"], end.get("attempt") or 1)
    if fk == "infrastructure_error":
        return "retry_exhausted" if inv["retries"] > 0 else "infra_unretried"
    if fk == "tool_input_error":
        return "tool_input_error"
    if end.get("status") == "success" or fk in (None, "none"):
        return "recovered_after_retry" if (attempts > 1 or inv["retries"] > 0) else "ok"
    return "tool_input_error"


def _subtype(end: dict | None) -> str | None:
    if not end:
        return None
    preview = end.get("output_preview") or ""
    for prefix, name in _SUBTYPE_PREFIXES:
        if preview.startswith(prefix):
            return name
    return "other"


def attribute(path: str) -> dict:
    events = load_events(path)
    invs, researcher_events = build_invocations(events)
    labels: dict[str, int] = defaultdict(int)
    by_tool: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bad_cases = []
    for inv in invs:
        label = classify_invocation(inv)
        labels[label] += 1
        tool = inv["key"][1]
        by_tool[tool][label] += 1
        if label not in ("ok", "recovered_after_retry"):
            end = inv["last_end"]
            case = {
                "label": label, "tool": tool, "researcher_id": inv["key"][0], "call_id": inv["key"][2],
                "attempts": max(inv["starts"], (end or {}).get("attempt") or 0), "retries": inv["retries"],
                "seqs": inv["seqs"], "output_preview": (end or {}).get("output_preview"),
            }
            if label == "tool_input_error":
                case["subtype_heuristic"] = _subtype(end)
            bad_cases.append(case)
    loop_by_reason: dict[str, int] = defaultdict(int)
    degradation_by_tool: dict[str, int] = defaultdict(int)
    for e in researcher_events:
        if e["event_type"] == "loop_detected":
            loop_by_reason[e.get("reason")] += 1
        else:
            degradation_by_tool[e.get("tool")] += 1
    return {
        "total_invocations": len(invs),
        "label_counts": dict(labels),
        "by_tool": {t: dict(v) for t, v in by_tool.items()},
        "bad_cases": sorted(bad_cases, key=lambda c: (c["seqs"][0] if c["seqs"] else 0)),
        "loop_detected_by_reason": dict(loop_by_reason),
        "degradation_by_tool": dict(degradation_by_tool),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = attribute(a.path)
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    print(f"工具调用总数: {r['total_invocations']}")
    print(f"归因标签分布: {r['label_counts']}")
    for t, v in r["by_tool"].items():
        print(f"  {t}: {v}")
    print(f"循环检测: {r['loop_detected_by_reason']}  降级: {r['degradation_by_tool']}")
    print(f"Bad Case 共 {len(r['bad_cases'])} 个:")
    for c in r["bad_cases"]:
        print(f"  [{c['label']}] {c['tool']} attempts={c['attempts']} seqs={c['seqs']} preview={c['output_preview']!r}")


if __name__ == "__main__":
    main()
