"""eval_budget.py —— 评测用的 token 用量统计与累计费用刹车（不属于业务代码，不改动 src/）。

设计边界（先写清楚，再写代码）：
1. 覆盖范围：通过 LangChain 回调挂在图的 config 上，覆盖所有走 LangChain 聊天模型的调用——
   澄清、研究简报、supervisor、researcher、压缩、最终报告、摘要，以及 with_retry 的每一次重试
   （每次重试都会重新触发回调）。**不覆盖**不经过 LangChain 的调用：mem0 自带的 OpenAI 客户端、
   Tavily 等搜索 API。runner 默认关闭记忆、关闭网页搜索来消除这两个盲区，见 agent_eval_run.py。
2. 两道检查，含义不同，不能混为一谈：
   - 请求前预检（on_*_start）：用"最坏情况估算"判断这次请求会不会让累计费用越线。
     输入 token 用 UTF-8 字节数 / bytes_per_token 估计（启发式，不是 tokenizer，不保证上界），
     输出 token 取请求里的 max_tokens（取不到就用 default_max_output_tokens）。
     并发请求按"在途预留"累计，避免多个并行请求各自通过预检后一起越线。
   - 响应后核算（on_llm_end）：用 API 真实返回的 usage 计费。若累计已超过上限，记录 overshoot，
     并锁死（latch）——**这次请求的费用已经发生，所以这不是绝对硬上限**，只能保证越线之后
     不再放行任何新请求。
3. 用量缺失不当作 0，也不静默继续：成功响应没有 usage 时，按最坏情况计费（估算输入 + max_tokens 全部用满），
   标记 usage_source="missing_estimated"，并且默认（on_missing_usage="halt"）**立即锁死**，要求人工检查。
   请求失败时：5xx / 超时 / 连接中断 / 被取消视为"可能已被计费"，按该请求最坏情况计入累计；4xx（限流、
   鉴权、参数错误）按"未计费"处理——后者是对 OpenAI 通常行为的假设，不是保证。
4. 价格表是手工维护的常量，来源见 PRICE_SOURCE_NOTE；模型不在价格表里时拒绝放行（fail closed），
   不会悄悄按 0 或按某个默认值计费。输入侧一律按未缓存价格计费（缓存命中会让真实费用更低，这是
   刻意的保守）。
5. BudgetExceeded 是普通 Exception：业务代码里有宽泛的 except（例如 supervisor_tools），会把它吞掉并
   继续走后续节点。因此"图是否正常结束"不能用来判断是否超预算——runner 必须检查 tracker.tripped。
   被拦截的请求不会到达模型，所以不产生费用；锁死后所有后续请求都被拦截。
"""

from __future__ import annotations

import json
import math
import re
import threading
from typing import Any, Optional

from langchain_core.callbacks import AsyncCallbackHandler

# (输入, 输出) 美元 / 百万 token。只收录已对照官方页核对过的模型；其他模型一律 fail closed（不放行）。
PRICES_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
}
# 仅供参考：缓存命中输入的官方价。计费时**不使用**（一律按未缓存输入价，偏保守，最多高估缓存命中的那部分）。
CACHED_INPUT_USD_PER_1M = {"gpt-4.1": 0.50, "gpt-4.1-mini": 0.10}
PRICE_SOURCE_NOTE = (
    "2026-10-12 核对 OpenAI 官方模型页：https://developers.openai.com/api/docs/models/gpt-4.1 "
    "(输入 2.00 / 缓存输入 0.50 / 输出 8.00) 与 .../gpt-4.1-mini (0.40 / 0.10 / 1.60)，标准档、文本 token；"
    "快照名 gpt-4.1-2025-04-14、gpt-4.1-mini-2025-04-14 归一到同名模型。价格会变，开跑当天请再看一眼。"
    "mini 页面没有显式写“standard tier”字样。其他模型（含 gpt-4o 系列）未核对，不在白名单内。"
)


class BudgetExceeded(Exception):
    """请求被预算刹车拦截（请求前预检失败，或已锁死）。注意：是普通 Exception，会被宽泛的 except 吞掉。"""


class UnknownModelPrice(BudgetExceeded):
    """模型不在价格表里：无法计费就不放行。"""


def normalize_model(name: Optional[str], prices: dict[str, tuple[float, float]] = PRICES_USD_PER_1M) -> Optional[str]:
    """把 'openai:gpt-4.1' / 'gpt-4.1-2025-04-14' 这类名字归一到价格表的键（最长前缀匹配）。"""
    if not name or not isinstance(name, str):
        return None
    n = name.split(":", 1)[-1].strip().lower()
    best = None
    for key in prices:
        if n == key or n.startswith(key + "-") or n.startswith(key + "@"):
            if best is None or len(key) > len(best):
                best = key
    return best


def _content_bytes(messages: Any) -> int:
    total = 0
    for m in messages or []:
        group = m if isinstance(m, (list, tuple)) else [m]
        for item in group:
            content = getattr(item, "content", item)
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            total += len(content.encode("utf-8"))
    return total


class BudgetTracker:
    def __init__(
        self,
        cap_usd: float,
        *,
        prices: dict[str, tuple[float, float]] = PRICES_USD_PER_1M,
        bytes_per_token: float = 2.0,
        default_max_output_tokens: int = 8192,
        on_missing_usage: str = "halt",
    ):
        if on_missing_usage not in ("halt", "estimate"):
            raise ValueError("on_missing_usage 只能是 'halt' 或 'estimate'")
        self.on_missing_usage = on_missing_usage
        if cap_usd <= 0:
            raise ValueError("cap_usd 必须为正")
        self.cap_usd = float(cap_usd)
        self.prices = prices
        self.bytes_per_token = float(bytes_per_token)
        self.default_max_output_tokens = int(default_max_output_tokens)
        self.records: list[dict[str, Any]] = []
        self.actual_usd = 0.0
        self.tripped = False
        self.trip_reason: Optional[str] = None
        self._inflight: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    # ---- 内部 ----
    def _price(self, model: Optional[str]) -> tuple[str, float, float]:
        key = normalize_model(model, self.prices)
        if key is None:
            raise UnknownModelPrice(f"模型 {model!r} 不在价格表中，无法计费，拒绝放行")
        p_in, p_out = self.prices[key]
        return key, p_in, p_out

    def _cost(self, key: str, in_tokens: int, out_tokens: int) -> float:
        p_in, p_out = self.prices[key]
        return in_tokens * p_in / 1e6 + out_tokens * p_out / 1e6

    def _record(self, **kw: Any) -> dict[str, Any]:
        self._seq += 1
        rec = {"seq": self._seq, **kw}
        self.records.append(rec)
        return rec

    @property
    def inflight_reserved_usd(self) -> float:
        return sum(v["worst_case_usd"] for v in self._inflight.values())

    # ---- 请求前预检 ----
    def preflight(self, run_id: str, model: Optional[str], messages: Any, max_output_tokens: Optional[int]) -> None:
        with self._lock:
            if self.tripped:
                self._record(run_id=run_id, model=model, status="blocked", reason="latched: " + str(self.trip_reason),
                             cost_usd=0.0, cumulative_usd=round(self.actual_usd, 6))
                raise BudgetExceeded(f"预算已锁死：{self.trip_reason}")
            try:
                key, p_in, p_out = self._price(model)
            except UnknownModelPrice as e:
                self.tripped, self.trip_reason = True, f"unknown_model:{model}"
                self._record(run_id=run_id, model=model, status="blocked", reason="unknown_model",
                             cost_usd=0.0, cumulative_usd=round(self.actual_usd, 6))
                raise e
            est_in = math.ceil(_content_bytes(messages) / self.bytes_per_token)
            max_out = int(max_output_tokens or self.default_max_output_tokens)
            worst = self._cost(key, est_in, max_out)
            projected = self.actual_usd + self.inflight_reserved_usd + worst
            if projected > self.cap_usd:
                self.tripped = True
                self.trip_reason = (f"preflight: 已花 {self.actual_usd:.4f} + 在途预留 {self.inflight_reserved_usd:.4f} "
                                    f"+ 本次最坏 {worst:.4f} > 上限 {self.cap_usd:.4f}")
                self._record(run_id=run_id, model=key, status="blocked", reason=self.trip_reason,
                             est_input_tokens=est_in, max_output_tokens=max_out, worst_case_usd=round(worst, 6),
                             cost_usd=0.0, cumulative_usd=round(self.actual_usd, 6))
                raise BudgetExceeded(self.trip_reason)
            self._inflight[run_id] = {"model": key, "worst_case_usd": worst, "est_input_tokens": est_in,
                                      "max_output_tokens": max_out, "input_bytes": _content_bytes(messages)}

    # ---- 响应后核算 ----
    def settle(self, run_id: str, response_model: Optional[str], input_tokens: Optional[int],
               output_tokens: Optional[int]) -> dict[str, Any]:
        with self._lock:
            pre = self._inflight.pop(run_id, None)
            if pre is None:  # 没经过预检（不应发生）：按响应模型计费，标记出来
                key = normalize_model(response_model, self.prices)
                pre = {"model": key, "worst_case_usd": 0.0, "est_input_tokens": 0, "max_output_tokens": 0,
                       "input_bytes": 0}
                note = "no_preflight"
            else:
                note = None
            key = normalize_model(response_model, self.prices) or pre["model"]
            if key is None:
                self.tripped, self.trip_reason = True, f"unknown_model_in_response:{response_model}"
                return self._record(run_id=run_id, model=response_model, status="ok_unpriced",
                                    usage_source="unpriced", cost_usd=None, cumulative_usd=round(self.actual_usd, 6))
            missing = input_tokens is None or output_tokens is None
            if missing:
                # 缺失 usage：按"最坏情况"计费（估算输入 + 请求的 max_tokens 全部用满），宁可高估，绝不记 0。
                source = "missing_estimated"
                in_t = max(pre["est_input_tokens"], int(input_tokens or 0))
                out_t = max(pre["max_output_tokens"], int(output_tokens or 0))
            else:
                source, in_t, out_t = "api", int(input_tokens), int(output_tokens)
            cost = self._cost(key, in_t, out_t)
            self.actual_usd += cost
            rec = self._record(run_id=run_id, model=key, status="ok", usage_source=source, input_tokens=in_t,
                               output_tokens=out_t, cost_usd=round(cost, 6), cumulative_usd=round(self.actual_usd, 6),
                               est_input_tokens=pre["est_input_tokens"], note=note)
            if missing and self.on_missing_usage == "halt" and not self.tripped:
                self.tripped = True
                self.trip_reason = ("usage_missing: 模型响应没有 usage，费用只能估算 —— 立即停止，需人工检查后再决定是否继续")
                rec["usage_missing_halt"] = True
            if self.actual_usd > self.cap_usd:
                overshoot = self.actual_usd - self.cap_usd
                self.tripped = True
                self.trip_reason = f"overshoot: 累计 {self.actual_usd:.4f} > 上限 {self.cap_usd:.4f}"
                rec["overshoot_usd"] = round(overshoot, 6)
            return rec

    @staticmethod
    def error_may_be_billed(error: BaseException) -> bool:
        """请求可能已被服务端处理/计费的失败：5xx、超时、连接中断、被取消（请求可能已发出）。
        4xx（限流、鉴权、参数错误）按未计费处理——这是对 OpenAI 通常行为的假设，不是保证。"""
        sc = getattr(error, "status_code", None)
        if isinstance(sc, int):
            return sc >= 500
        name = type(error).__name__
        return any(w in name for w in ("Timeout", "Connection", "Connect", "Remote", "Cancel", "Internal", "Disconnect"))

    def fail(self, run_id: str, model: Optional[str], error: BaseException) -> None:
        with self._lock:
            pre = self._inflight.pop(run_id, None)
            m = (pre or {}).get("model") or model
            if pre is not None and self.error_may_be_billed(error):
                # 费用未知：按该请求的最坏情况计入累计（保守），并明确标记
                cost = pre["worst_case_usd"]
                self.actual_usd += cost
                rec = self._record(run_id=run_id, model=m, status="error", usage_source="error_cost_unknown_charged_worst_case",
                                   error=type(error).__name__, cost_usd=round(cost, 6),
                                   cumulative_usd=round(self.actual_usd, 6))
                if self.actual_usd > self.cap_usd:
                    self.tripped = True
                    self.trip_reason = f"overshoot: 累计 {self.actual_usd:.4f} > 上限 {self.cap_usd:.4f}"
                    rec["overshoot_usd"] = round(self.actual_usd - self.cap_usd, 6)
            else:
                self._record(run_id=run_id, model=m, status="error", usage_source="error_assumed_not_billed",
                             error=type(error).__name__, cost_usd=0.0, cumulative_usd=round(self.actual_usd, 6))

    # ---- 汇总 ----
    def summary(self) -> dict[str, Any]:
        ok = [r for r in self.records if r["status"] == "ok"]
        charged_errors = round(sum(r["cost_usd"] for r in self.records
                                   if str(r.get("usage_source", "")).startswith("error_cost_unknown")), 6)
        by_model: dict[str, dict[str, Any]] = {}
        for r in ok:
            d = by_model.setdefault(r["model"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            d["calls"] += 1
            d["input_tokens"] += r["input_tokens"]
            d["output_tokens"] += r["output_tokens"]
            d["cost_usd"] = round(d["cost_usd"] + r["cost_usd"], 6)
        return {
            "cap_usd": self.cap_usd,
            "actual_usd": round(self.actual_usd, 6),
            "tripped": self.tripped,
            "trip_reason": self.trip_reason,
            "calls_ok": len(ok),
            "calls_blocked": sum(1 for r in self.records if r["status"] == "blocked"),
            "calls_error_assumed_not_billed": sum(1 for r in self.records if r.get("usage_source") == "error_assumed_not_billed"),
            "calls_error_cost_unknown_charged_worst_case": sum(
                1 for r in self.records if str(r.get("usage_source", "")).startswith("error_cost_unknown")),
            "on_missing_usage": self.on_missing_usage,
            "usage_missing_halt": any(r.get("usage_missing_halt") for r in self.records),
            "calls_usage_missing_estimated": sum(1 for r in ok if r["usage_source"] == "missing_estimated"),
            "overshoot_usd": max([r.get("overshoot_usd", 0.0) for r in self.records] or [0.0]),
            "charged_for_failed_requests_usd": charged_errors,
            "by_model": by_model,
            "price_source_note": PRICE_SOURCE_NOTE,
        }

    def dump_jsonl(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for r in self.records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _model_from_invocation(kwargs: dict, serialized: Optional[dict], metadata: Optional[dict]) -> Optional[str]:
    inv = kwargs.get("invocation_params") or {}
    for k in ("model", "model_name", "model_id"):
        if isinstance(inv.get(k), str):
            return inv[k]
    if isinstance(metadata, dict) and isinstance(metadata.get("ls_model_name"), str):
        return metadata["ls_model_name"]
    return None


def _max_out_from_invocation(kwargs: dict, metadata: Optional[dict]) -> Optional[int]:
    inv = kwargs.get("invocation_params") or {}
    for k in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
        if isinstance(inv.get(k), int):
            return inv[k]
    if isinstance(metadata, dict) and isinstance(metadata.get("ls_max_tokens"), int):
        return metadata["ls_max_tokens"]
    return None


_FAKE_ID = re.compile(r"[^0-9a-zA-Z_-]")


class BudgetCallback(AsyncCallbackHandler):
    """挂在图的 config["callbacks"] 上。raise_error=True 让预检异常真正中断调用，而不是被回调管理器吞掉。"""

    raise_error = True
    run_inline = True

    def __init__(self, tracker: BudgetTracker):
        self.tracker = tracker
        self._models: dict[str, Optional[str]] = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None,
                                  metadata=None, **kwargs):
        model = _model_from_invocation(kwargs, serialized, metadata)
        self._models[str(run_id)] = model
        self.tracker.preflight(str(run_id), model, messages, _max_out_from_invocation(kwargs, metadata))

    async def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        model = _model_from_invocation(kwargs, serialized, metadata)
        self._models[str(run_id)] = model
        self.tracker.preflight(str(run_id), model, prompts, _max_out_from_invocation(kwargs, metadata))

    async def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        in_t = out_t = None
        resp_model = None
        try:
            for gens in response.generations:
                for g in gens:
                    msg = getattr(g, "message", None)
                    um = getattr(msg, "usage_metadata", None)
                    if um and um.get("input_tokens") is not None and um.get("output_tokens") is not None:
                        in_t = (in_t or 0) + int(um["input_tokens"])
                        out_t = (out_t or 0) + int(um["output_tokens"])
                    rm = getattr(msg, "response_metadata", None) or {}
                    resp_model = resp_model or rm.get("model_name") or rm.get("model")
            if in_t is None and isinstance(response.llm_output, dict):
                tu = response.llm_output.get("token_usage") or {}
                if "prompt_tokens" in tu and "completion_tokens" in tu:
                    in_t, out_t = int(tu["prompt_tokens"]), int(tu["completion_tokens"])
            if resp_model is None and isinstance(response.llm_output, dict):
                resp_model = response.llm_output.get("model_name")
        except Exception:
            in_t = out_t = None  # 解析失败 → 当作缺失，走估算并标记，不当作 0
        self.tracker.settle(str(run_id), resp_model or self._models.get(str(run_id)), in_t, out_t)

    async def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        if isinstance(error, BudgetExceeded):
            return  # 预检失败时已记录 blocked，没有在途预留
        self.tracker.fail(str(run_id), self._models.get(str(run_id)), error)
