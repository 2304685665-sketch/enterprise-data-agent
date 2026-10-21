"""模型降级：主模型调用在"可恢复的服务端/网络故障"下失败时，依次尝试备用模型。

只对这类故障降级：连接失败、超时、限流（429）、服务端 5xx。请求本身有问题（认证失败、
参数错误、上下文超长等 4xx）不降级——换一个模型不会让错误的请求变对，反而会掩盖配置问题，
所以这类异常原样抛出。

局限：备用模型的能力/风格可能与主模型不同，降级后输出质量没有保证；本模块只保证"可用性"，
并如实返回是哪一个模型最终提供了响应（调用方应记录）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


def _fallback_worthy_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [asyncio.TimeoutError, ConnectionError]
    try:
        import openai

        types += [
            openai.APIConnectionError,  # 含 APITimeoutError（子类）
            openai.RateLimitError,
            openai.InternalServerError,
        ]
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def is_fallback_worthy(exc: BaseException) -> bool:
    return isinstance(exc, _fallback_worthy_types())


class AllModelsFailed(Exception):
    """所有候选模型都因可降级故障失败；errors 为 [(模型标签, 异常)]。"""

    def __init__(self, errors: list[tuple[str, BaseException]]):
        self.errors = errors
        super().__init__("; ".join(f"{label}: {type(e).__name__}" for label, e in errors))


async def ainvoke_with_fallback(
    candidates: Sequence[tuple[str, Any]],
    messages: Any,
    is_worthy: Callable[[BaseException], bool] = is_fallback_worthy,
) -> tuple[Any, str]:
    """按顺序调用候选（标签, 可 ainvoke 的对象）。返回 (响应, 提供响应的标签)。
    不可降级的异常立即原样抛出；全部候选都因可降级故障失败时抛 AllModelsFailed。"""
    if not candidates:
        raise ValueError("candidates must not be empty")
    errors: list[tuple[str, BaseException]] = []
    for label, model in candidates:
        try:
            return await model.ainvoke(messages), label
        except Exception as e:  # noqa: BLE001
            if not is_worthy(e):
                raise
            logger.warning("model %s failed with %s; trying next candidate", label, type(e).__name__)
            errors.append((label, e))
    raise AllModelsFailed(errors)
