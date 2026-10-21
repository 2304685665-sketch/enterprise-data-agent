"""summarize_trace.py —— 对run_trace.py产出的一个JSONL轨迹文件做统计汇总。

用法：
    .venv/bin/python3 summarize_trace.py <path-to-run_trace.jsonl> [--json]

统计内容：
- 总事件数（文件总行数）
- 各工具调用次数（按tool_start事件分组计数）
- 失败按failure_kind分布（所有tool_end事件，包括"none"这个"没失败"的桶，
  这样才能看出失败占比而不是只看到失败数字本身）
- 重试次数（retry事件数）
- 熔断拦截次数（breaker_block事件数）
- 降级事件数（degradation事件数）
- 循环检测命中总数+按reason（repeat/abab/force_stop）分布（B1新增）
- 各工具耗时p50/p95（只用tool_end里duration_ms非None的事件，按tool分组；
  线性插值法，和numpy默认percentile算法一致，对小样本是确定性的）
- 未配对的tool_start数量 + 未配对的tool_end数量（两个数都报，不只报一个）：
  配对规则——
    * call_id非空时，按(researcher_id, tool, call_id)精确分组：这是真实
      身份匹配（call_id复用模型自己产生的tool_call id），**逐次归因**，
      不是统计意义上的凑数。
    * call_id为空（比如直接拿run_trace.emit()做单元测试、没有经过
      researcher_tools这条真实调用链）时，退化成按(researcher_id, tool)
      分组——这种情况下只能"按数量配对，不保证逐次归因"：如果同一个
      (researcher_id, tool)下短时间内有多个独立的tool_call，配对结果
      分不清究竟是哪一次对应哪一次，只能保证"数量对得上"。
  组内再按seq顺序FIFO配对tool_start/tool_end（tool_end带attempt时优先
  精确匹配同attempt的tool_start，匹配不到——包括外层超时补发的tool_end
  本身没带attempt的情况——就退化成配对最早挂着的那个）。
  一直挂到文件末尾没配上的tool_start计入未配对tool_start；出现时pending
  队列已经空了的tool_end计入未配对tool_end。两者都不隐藏（对应B3审批
  要求）。
"""

import argparse
import json
import sys
from collections import defaultdict


def _percentile(values: list, pct: float):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return d0 + d1


def summarize(path: str) -> dict:
    events = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))

    tool_start_calls = defaultdict(int)
    failure_kind_dist = defaultdict(int)
    durations_by_tool = defaultdict(list)
    retry_count = 0
    breaker_block_count = 0
    degradation_count = 0
    loop_detected_count = 0
    loop_detected_by_reason = defaultdict(int)  # B1新增：repeat/abab/force_stop各自的命中次数

    # 配对算法：call_id非空时按(researcher_id, tool, call_id)精确分组
    # （逐次归因）；call_id为空时退化成按(researcher_id, tool)分组
    # （只按数量配对，不保证逐次归因——见模块文档字符串）。组内再按seq
    # 顺序维护一个"还没配对的tool_start"队列（FIFO）：tool_end带着明确的
    # attempt时优先精确匹配同attempt的那个；匹配不到（或者这条tool_end
    # 本身没有attempt——外层超时补发的那种）就退化成"配对最早挂着的那个"。
    # 单个工具调用在execute_tool_safely内部的重试是严格顺序执行的（不是
    # 并发跑多个attempt），FIFO语义本身是对的，不是凑出来让测试过的权宜
    # 之计。
    open_starts = defaultdict(list)  # group_key -> [attempt, ...] 按出现顺序
    unpaired_tool_ends = 0

    def _group_key(e):
        call_id = e.get("call_id")
        if call_id:
            return (e.get("researcher_id"), e.get("tool"), call_id)
        return (e.get("researcher_id"), e.get("tool"))

    events_sorted = sorted(events, key=lambda e: e.get("seq", 0))
    for e in events_sorted:
        et = e.get("event_type")
        group_key = _group_key(e)
        if et == "tool_start":
            tool_start_calls[e.get("tool")] += 1
            open_starts[group_key].append(e.get("attempt"))
        elif et == "tool_end":
            failure_kind_dist[e.get("failure_kind")] += 1
            if e.get("duration_ms") is not None:
                durations_by_tool[e.get("tool")].append(e["duration_ms"])
            pending = open_starts[group_key]
            attempt = e.get("attempt")
            if attempt is not None and attempt in pending:
                pending.remove(attempt)
            elif pending:
                pending.pop(0)
            else:
                unpaired_tool_ends += 1
        elif et == "retry":
            retry_count += 1
        elif et == "breaker_block":
            breaker_block_count += 1
        elif et == "degradation":
            degradation_count += 1
        elif et == "loop_detected":
            loop_detected_count += 1
            loop_detected_by_reason[e.get("reason")] += 1

    unpaired_tool_starts = sum(len(v) for v in open_starts.values())

    duration_stats = {}
    for tool, values in durations_by_tool.items():
        duration_stats[tool] = {
            "p50_ms": _percentile(values, 50),
            "p95_ms": _percentile(values, 95),
            "count": len(values),
        }

    return {
        "total_events": len(events),
        "tool_call_counts": dict(tool_start_calls),
        "failure_kind_distribution": dict(failure_kind_dist),
        "retry_count": retry_count,
        "breaker_block_count": breaker_block_count,
        "degradation_count": degradation_count,
        "loop_detected_count": loop_detected_count,
        "loop_detected_by_reason": dict(loop_detected_by_reason),
        "duration_ms_by_tool": duration_stats,
        "unpaired_tool_start_count": unpaired_tool_starts,
        "unpaired_tool_end_count": unpaired_tool_ends,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="输出JSON而不是人类可读文本")
    args = parser.parse_args()

    result = summarize(args.path)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(f"总事件数: {result['total_events']}")
    print(f"各工具调用次数: {result['tool_call_counts']}")
    print(f"失败分布（按failure_kind）: {result['failure_kind_distribution']}")
    print(f"重试次数: {result['retry_count']}")
    print(f"熔断拦截次数: {result['breaker_block_count']}")
    print(f"降级事件数: {result['degradation_count']}")
    print(f"循环检测命中总数: {result['loop_detected_count']}（按reason分布: {result['loop_detected_by_reason']}）")
    print("各工具耗时:")
    for tool, stats in result["duration_ms_by_tool"].items():
        print(f"  {tool}: p50={stats['p50_ms']:.1f}ms p95={stats['p95_ms']:.1f}ms (n={stats['count']})")
    print(f"未配对的tool_start数量: {result['unpaired_tool_start_count']}")
    print(f"未配对的tool_end数量: {result['unpaired_tool_end_count']}")


if __name__ == "__main__":
    main()
