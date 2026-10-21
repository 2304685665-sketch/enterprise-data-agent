"""端到端故障注入臂（只在评测进程里生效，不改源码文件）。

对象：真实 Agent 图里 internal_doc_search 的"抛异常版"入口（_RETRY_AWARE_TOOL_CALLS），
即 execute_tool_safely 实际调用的那个函数。每次尝试以概率 p 抛 TransientToolError（与真实网络类瞬时错误同一类型），
否则调用原实现。种子按 (seed, 用例id) 固定，所以不同臂在同一用例上看到的是同一条故障序列的前缀。
记录：每条用例的尝试次数、注入次数、放行次数 → result["fault"]。
重试次数由 --fault-retries 通过 Configuration.tool_retry_max_attempts 传入（0 = 不重试）。
"""
from __future__ import annotations

import hashlib
import random

from open_deep_research.tool_reliability import TransientToolError

TOOL = "internal_doc_search"


def install(dr, p: float, seed: int):
    orig = dr._RETRY_AWARE_TOOL_CALLS[TOOL]
    state = {"rng": random.Random(0), "attempts": 0, "injected": 0}

    async def faulty(*a, **k):
        state["attempts"] += 1
        if state["rng"].random() < p:
            state["injected"] += 1
            raise TransientToolError("injected transient failure")
        return await orig(*a, **k)

    dr._RETRY_AWARE_TOOL_CALLS[TOOL] = faulty

    def pre(case):
        h = hashlib.sha256(f"{seed}:{case['id']}".encode()).hexdigest()
        state.update(rng=random.Random(int(h[:12], 16)), attempts=0, injected=0)

    def post(case, result):
        result["fault"] = {"p": p, "seed": seed, "doc_search_attempts": state["attempts"], "injected": state["injected"],
                           "passed_through": state["attempts"] - state["injected"]}

    return pre, post


def chain(pre_a, post_a, pre_b, post_b):
    """先 a 后 b；pre 的返回值取 a 的（数据库路径）。"""
    def pre(case):
        r = pre_a(case) if pre_a else None
        pre_b(case)
        return r

    def post(case, result):
        if post_a:
            post_a(case, result)
        post_b(case, result)
    return pre, post
