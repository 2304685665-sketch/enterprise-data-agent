"""重试层的离线故障注入模拟（合成数据，不是线上数据）。

对象：真实的 execute_tool_safely（重试/退避/失败分类），不是它的复刻。
做法：注册一个假的"抛异常版"工具入口，让它按固定随机种子、以概率 p 抛 TransientToolError，
在不同的最大重试次数 r 下各调用 N 次，统计最终成功率与平均尝试次数，并与独立同分布假设下的
解析值对照：成功率 = 1 - p^(r+1)。

这能说明什么、不能说明什么：
- 能说明：重试层的实现与"瞬时错误独立同分布"下的理论一致，且上限/分类行为正确
  （最多 r+1 次尝试；永久错误不被重试；重试耗尽归为 infrastructure_error）。
- 不能说明：真实线上故障是否独立同分布（真实故障常成片出现，重试收益会低于这里）；
  也不覆盖熔断（熔断在 researcher_tools 的状态里，另有测试）。结果只能称为"合成故障注入"。

用法：PYTHONPATH=src python my_extensions/sim_fault_injection.py [--n 2000] [--out PATH]
"""

import argparse
import asyncio
import json
import os
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-sim")

from open_deep_research import deep_researcher as dr  # noqa: E402
from open_deep_research.tool_reliability import TransientToolError  # noqa: E402

SIM_TOOL_NAME = "sim_tool"


class _SimTool:
    name = SIM_TOOL_NAME

    async def ainvoke(self, args, config=None):  # 不会被调用：注册了抛异常版入口
        raise AssertionError("should use the raising entry")


async def run_cell(p: float, max_retries: int, n: int, seed: int, permanent: bool = False) -> dict:
    rng = random.Random(seed)

    async def raising_call(**kwargs):
        if rng.random() < p:
            if permanent:
                raise ValueError("permanent failure")
            raise TransientToolError("simulated transient failure")
        return "ok"

    dr._RETRY_AWARE_TOOL_CALLS[SIM_TOOL_NAME] = raising_call
    cfg = SimpleNamespace(tool_retry_max_attempts=max_retries, tool_retry_base_delay_seconds=0.0)
    ok = 0
    attempts_total = 0
    max_attempts = 0
    kinds: dict[str, int] = {}
    try:
        for _ in range(n):
            content, status, kind, attempts = await dr.execute_tool_safely(
                _SimTool(), {}, config=None, configurable=cfg
            )
            ok += status == "success"
            attempts_total += attempts
            max_attempts = max(max_attempts, attempts)
            kinds[kind] = kinds.get(kind, 0) + 1
    finally:
        dr._RETRY_AWARE_TOOL_CALLS.pop(SIM_TOOL_NAME, None)
    return {
        "p": p, "max_retries": max_retries, "n": n,
        "success_rate": ok / n,
        "analytical_success_rate": 1 - p ** (max_retries + 1),
        "mean_attempts": attempts_total / n,
        "max_attempts": max_attempts,
        "failure_kinds": kinds,
    }


async def run_all(n: int, seed: int = 20261012) -> list[dict]:
    rows = []
    for p in (0.1, 0.3, 0.5):
        for r in (0, 1, 2, 3):
            rows.append(await run_cell(p, r, n, seed))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results", "sim_fault_injection.json"))
    a = ap.parse_args()
    rows = asyncio.run(run_all(a.n))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"note": "synthetic i.i.d. fault injection on real execute_tool_safely; not production data", "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"{'p':>5} {'retries':>8} {'success':>9} {'analytic':>9} {'mean_att':>9}")
    for r in rows:
        print(f"{r['p']:>5} {r['max_retries']:>8} {r['success_rate']:>9.3f} {r['analytical_success_rate']:>9.3f} {r['mean_attempts']:>9.3f}")


if __name__ == "__main__":
    main()
