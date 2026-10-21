"""会话级故障注入（合成数据）：600 秒虚拟时间、约 30% 故障，同时覆盖重试与熔断。

和 sim_fault_injection.py 的区别：那个只测 execute_tool_safely 的重试，且故障独立同分布，
结果与公式 1-p^(r+1) 吻合。这里驱动的是**真实的 researcher_tools**（含熔断计数、重试、退避），
在虚拟时钟上按固定到达时间发起调用，并比较三种配置：

  no_retry      : 重试 0 次，熔断关闭（阈值极大）
  retry         : 默认重试（2 次，指数退避 0.5s），熔断关闭
  retry+breaker : 默认重试 + 默认熔断阈值（3 次连续基础设施失败，任务内生效）

两种故障模型（都约 30% 的故障强度）：
  iid   : 每次尝试独立以 p=0.30 失败
  burst : 按时间成片的故障窗口（交替的正常/故障区间，均值 35s/15s），窗口内所有尝试都失败

工作负载：每 2 秒到达一次调用，每 5 次调用算一个“研究任务”（熔断计数按任务重置），共 300 次调用。

诚实边界：
- 合成数据；工具是假的，只会抛 TransientToolError 或成功；退避延迟用虚拟时钟计时。
- 熔断的作用是减少对已坏工具的无效尝试和等待，并如实暴露降级——它**不会**提高调用成功率，
  在 iid 故障下甚至会因为“任务内暂时标记不可用”而降低成功率。结果里两种都会如实报告。
- 重试在 burst 故障下收益会明显低于 iid（退避总时长远小于故障窗口）。

用法：PYTHONPATH=src python my_extensions/sim_session_fault_injection.py [--seeds 30] [--out PATH]
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
from unittest.mock import patch

from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-sim")

from open_deep_research import deep_researcher as dr  # noqa: E402
from open_deep_research.tool_reliability import TransientToolError  # noqa: E402

TOOL = "python_executor"  # 只借用它的名字；入口被替换为假的“抛异常版”
HORIZON = 600.0
CALL_INTERVAL = 2.0
CALLS_PER_TASK = 5
CALL_LATENCY = 0.3
NO_BREAKER = 10**9

CONFIGS = {
    "no_retry": {"tool_retry_max_attempts": 0, "tool_circuit_breaker_threshold": NO_BREAKER},
    "retry": {"tool_retry_max_attempts": 2, "tool_circuit_breaker_threshold": NO_BREAKER},
    "retry+breaker": {"tool_retry_max_attempts": 2, "tool_circuit_breaker_threshold": 3},
}

_real_sleep = asyncio.sleep


class Clock:
    def __init__(self):
        self.t = 0.0


def outage_windows(rng: random.Random, horizon: float, mean_up: float = 35.0, mean_down: float = 15.0):
    """交替的正常/故障区间；返回故障区间列表。"""
    t, down, windows = 0.0, False, []
    while t < horizon:
        d = rng.expovariate(1.0 / (mean_down if down else mean_up))
        if down:
            windows.append((t, min(t + d, horizon)))
        t += d
        down = not down
    return windows


def _in_windows(t, windows):
    return any(a <= t < b for a, b in windows)


async def run_session(config_name: str, fault_model: str, seed: int, horizon: float = HORIZON) -> dict:
    rng = random.Random(seed)  # 同一 seed 下三种配置面对同一份故障时间线（burst）/同一随机序列起点（iid）
    windows = outage_windows(random.Random(seed * 7919 + 1), horizon) if fault_model == "burst" else []
    clock = Clock()
    stats = {"attempts": 0, "attempt_failures": 0}

    async def fake_sleep(delay, *a, **k):
        clock.t += delay
        await _real_sleep(0)

    async def tool_call(code):
        clock.t += CALL_LATENCY
        stats["attempts"] += 1
        failed = (rng.random() < 0.30) if fault_model == "iid" else _in_windows(clock.t, windows)
        if failed:
            stats["attempt_failures"] += 1
            raise TransientToolError("simulated failure")
        return "ok"

    overrides = dict(CONFIGS[config_name], tool_retry_base_delay_seconds=0.5)
    n_calls = int(horizon / CALL_INTERVAL)
    ok = blocked = 0
    latencies, task_ok = [], []
    counts: dict = {}
    this_task_ok = True
    with patch.dict(dr._RETRY_AWARE_TOOL_CALLS, {TOOL: tool_call}), patch.object(asyncio, "sleep", fake_sleep):
        for i in range(n_calls):
            if i % CALLS_PER_TASK == 0:
                if i:
                    task_ok.append(this_task_ok)
                counts, this_task_ok = {}, True
            clock.t = max(clock.t, i * CALL_INTERVAL)  # 开环到达：不会因为前面慢就推迟故障时间线
            start = clock.t
            msg = AIMessage(content="", tool_calls=[{"name": TOOL, "args": {"code": "1"}, "id": str(i)}])
            state = {"researcher_messages": [msg], "tool_call_iterations": 0, "research_topic": "sim",
                     "compressed_research": "", "raw_notes": [], "tool_failure_counts": counts}
            res = await dr.researcher_tools(state, {"configurable": overrides})
            counts = res.update["tool_failure_counts"]
            m = res.update["researcher_messages"][0]
            success = getattr(m, "status", None) != "error"
            if "temporarily marked unavailable" in str(m.content):
                blocked += 1
            ok += success
            this_task_ok &= success
            latencies.append(clock.t - start)
        task_ok.append(this_task_ok)
    lat = sorted(latencies)
    return {
        "config": config_name, "fault_model": fault_model, "seed": seed, "calls": n_calls,
        "call_success_rate": ok / n_calls, "blocked_by_breaker": blocked,
        "attempts": stats["attempts"], "attempt_failure_rate": stats["attempt_failures"] / max(1, stats["attempts"]),
        "tasks": len(task_ok), "task_full_success_rate": sum(task_ok) / len(task_ok),
        "latency_p50_s": lat[len(lat) // 2], "latency_p95_s": lat[int(len(lat) * 0.95) - 1],
        "outage_fraction": (sum(b - a for a, b in windows) / horizon) if fault_model == "burst" else None,
    }


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for fm in ("iid", "burst"):
        for cfg in CONFIGS:
            sel = [r for r in rows if r["fault_model"] == fm and r["config"] == cfg]
            def mm(k):
                v = [r[k] for r in sel]
                return {"mean": statistics.mean(v), "min": min(v), "max": max(v)}
            out.append({"fault_model": fm, "config": cfg, "n_seeds": len(sel),
                        **{k: mm(k) for k in ("call_success_rate", "task_full_success_rate", "attempts",
                                              "blocked_by_breaker", "latency_p95_s")}})
    return out


async def run_all(n_seeds: int) -> list[dict]:
    rows = []
    for fm in ("iid", "burst"):
        for cfg in CONFIGS:
            for s in range(n_seeds):
                rows.append(await run_session(cfg, fm, 20261012 + s))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results", "sim_session_fault_injection.json"))
    a = ap.parse_args()
    rows = asyncio.run(run_all(a.seeds))
    summ = summarize(rows)
    if os.path.exists(a.out):
        raise SystemExit(f"拒绝覆盖已有结果: {a.out}")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"note": "synthetic session-level fault injection on real researcher_tools; virtual clock; not production data",
                   "summary": summ, "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"{'fault':>6} {'config':>14} {'call_ok':>8} {'task_ok':>8} {'attempts':>9} {'blocked':>8} {'p95_lat':>8}")
    for s in summ:
        print(f"{s['fault_model']:>6} {s['config']:>14} {s['call_success_rate']['mean']:>8.3f} "
              f"{s['task_full_success_rate']['mean']:>8.3f} {s['attempts']['mean']:>9.1f} "
              f"{s['blocked_by_breaker']['mean']:>8.1f} {s['latency_p95_s']['mean']:>8.2f}")


if __name__ == "__main__":
    main()
