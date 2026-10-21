"""run_trace.py —— 可选的本地JSONL执行轨迹记录（B3新增，默认关闭）。

设计原则（对应审批时的要求）：
- 默认关闭。开启需要同时满足两个条件：configurable.run_trace_enabled 为真
  （或环境变量 ODR_RUN_TRACE_ENABLED=1），**并且** configurable.run_trace_dir
  被显式配置——只置位enabled而不配目录，视为未启用（记一条warning，不写
  任何文件，不报错）。没有默认目录。
- 三个独立的身份字段：
    - researcher_id：由 supervisor_tools 的并行分发循环为每一次并行调用
      生成（仅在启用追踪时才生成/传递），标识"这是哪一个并行researcher"。
    - run_id：只在调用方自己在 config.configurable.run_trace_run_id 里
      提供时才沿用，本模块从不自己发明一个——留空（""）表示调用方没提供。
    - call_id（2026-10 审查后新增）：复用 LangChain 工具调用自带的
      tool_call["id"]（模型产生工具调用时自己生成的那个id，ToolMessage.
      tool_call_id用的就是它），不是本模块另起一套生成逻辑。由
      researcher_tools 的 _run_one 在调用链路最外层、仅在启用追踪时构造
      一份config副本塞进去（和researcher_id同一种做法），execute_tool_
      safely内部的emit调用因此也能读到同一个call_id，不需要改任何函数
      签名。这是summarize_trace.py做精确tool_start/tool_end配对的依据——
      没有call_id时才退化成按数量/顺序的弱配对（见summarize_trace.py）。
  三者互不替代：run_id是"外部调用方自己定义的大范围关联key"（比如一次
  完整的deep_researcher顶层调用），researcher_id是"这批并行researcher
  里的哪一个"，call_id是"这一个researcher内部的哪一次具体工具调用"。
- fail-open：写入失败（目录不可写、磁盘满等任何IO异常）只记一条warning
  日志，绝不抛异常、绝不影响调用方（execute_tool_safely/researcher_tools/
  supervisor_tools）的任何返回值或控制流。
- 并发安全：每条事件用 os.open(O_APPEND|O_CREAT|O_WRONLY) 打开、
  os.write() 写整行字节、os.close()，一次性完成，不保留长期打开的fd、
  不用缓冲的文件对象跨await点——同一进程内多个asyncio协程并发调用emit()
  时，各自的os.write()之间不会发生字节级交织（O_APPEND在POSIX上保证单次
  write()相对文件末尾的原子性；这里又保证每次write()就是一整行，两者
  叠加起来就是"整行写入不交织"）。
- 脱敏：参数只存sha256，从不存原始参数内容；输出只存经过正则脱敏、
  截断到200字符的预览，加上（脱敏后文本的）sha256。脱敏规则是
  best-effort的几个常见密钥格式（OpenAI风格sk-xxx、Bearer token、
  AWS风格AKIA...等），**不是**完整的secret scanner，没有能力保证所有
  格式的密钥都被过滤掉——这是边界，不是承诺，必须让使用者知道。
- seq 用 itertools.count()：是**进程内全局**单调递增序列，不是单次运行
  （单个run_id/researcher_id）内部的序号——同一进程里如果跑多次独立的
  研究任务，seq会继续往上涨，不会在新任务开始时归零。排序/去重时应该
  用 (researcher_id, seq) 或 (run_id, seq) 组合键，不能只看seq本身。

已知局限（如实记录，不回避）：
- asyncio.wait_for触发的外层超时取消：researcher_tools在except
  asyncio.TimeoutError分支里补发一条status="outer_timeout"的tool_end
  （本模块不负责，是deep_researcher.py里的调用点负责），弥补execute_
  tool_safely内部那次被取消的attempt来不及自己发tool_end的缺口。但如果
  取消恰好发生在两次重试之间的backoff sleep窗口（而不是某次真实执行
  中），这条补发的tool_end就没有对应的tool_start——会成为一条"未配对的
  tool_end"，这不在summarize_trace.py当前统计的"未配对tool_start"范围内，
  是已知的、没有进一步处理的边界情况。
- 不记录LLM调用的usage_metadata（本期范围只覆盖工具调用，见审批前的
  Step 0说明）。
"""

import hashlib
import itertools
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

_trace_logger = logging.getLogger("open_deep_research.run_trace")

# 进程内全局单调序号——见模块文档字符串关于"不是单次运行内序号"的说明。
_seq_counter = itertools.count()

# best-effort密钥格式脱敏，只覆盖几种常见、好识别的格式。
_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)api[_-]?key[\"'\s:=]+[A-Za-z0-9_\-]{16,}"),
]

_warned_missing_dir = False


def _redact(text: str) -> str:
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _safe_configurable(config) -> dict:
    """从任意形状的config里尽量取出configurable字典，取不到/类型不对就
    返回空字典，绝不抛异常。覆盖的畸形输入：config本身不是dict（包括
    None）、configurable键缺失、configurable的值是None、configurable的
    值根本不是dict（比如字符串/整数）。"""
    try:
        if not isinstance(config, dict):
            return {}
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            return {}
        return configurable
    except Exception:
        return {}


def _is_enabled_flag(config) -> bool:
    try:
        configurable = _safe_configurable(config)
        if bool(configurable.get("run_trace_enabled", False)):
            return True
        return os.environ.get("ODR_RUN_TRACE_ENABLED") == "1"
    except Exception:
        return False


def resolve_trace_dir(config) -> "str | None":
    """开启追踪且目录配置齐全时返回目录路径；否则返回None（视为未启用）。
    "启用但没配目录"这个情况只warning一次（避免日志被刷屏），不是每次
    emit()调用都重复警告。任何输入（包括config本身不是dict、configurable
    不是dict、run_trace_dir不是字符串）都不应该抛异常，统一按"未启用"
    处理——这是2026-10审查后加固的，之前只靠emit()自己的try/except兜底，
    没有保证is_enabled/resolve_trace_dir单独调用时也不会抛。
    """
    global _warned_missing_dir
    try:
        if not _is_enabled_flag(config):
            return None
        configurable = _safe_configurable(config)
        trace_dir = configurable.get("run_trace_dir")
        if not isinstance(trace_dir, str) or not trace_dir:
            if not _warned_missing_dir:
                _trace_logger.warning(
                    "run_trace_enabled=True但未配置有效的run_trace_dir（必须是非空字符串），"
                    "本次运行按未启用处理，不写任何文件。"
                )
                _warned_missing_dir = True
            return None
        return trace_dir
    except Exception:
        return None


def is_enabled(config) -> bool:
    """给supervisor_tools判断"要不要构造config副本"用——和resolve_trace_dir
    共享同一套判断，只是只要bool结果，不需要目录字符串本身。"""
    try:
        return resolve_trace_dir(config) is not None
    except Exception:
        return False


def new_id() -> str:
    try:
        import uuid
        return str(uuid.uuid4())
    except Exception:
        return ""


def _write_line(path: str, line: str) -> None:
    data = (line + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def emit(
    event_type: str,
    *,
    config: dict,
    research_topic: str = "",
    tool: "str | None" = None,
    attempt: "int | None" = None,
    duration_ms: "float | None" = None,
    status: "str | None" = None,
    failure_kind: "str | None" = None,
    reason: "str | None" = None,
    params: "dict | None" = None,
    output: "str | None" = None,
) -> None:
    """唯一的写入入口。关闭时在最前面就return，不做任何计算/IO。
    fail-open：任何异常（目录不可写、磁盘满、权限问题、config本身是畸形
    数据等）只warning，不抛——2026-10审查后把resolve_trace_dir(config)
    这次调用也挪进了try块内：虽然resolve_trace_dir自己已经不会抛异常，
    这里再包一层纯粹是双重保险，不依赖调用方自己在外面套try。
    """
    try:
        trace_dir = resolve_trace_dir(config)
        if trace_dir is None:
            return

        configurable = _safe_configurable(config)
        event = {
            "run_id": configurable.get("run_trace_run_id", ""),
            "researcher_id": configurable.get("run_trace_researcher_id", ""),
            "call_id": configurable.get("run_trace_call_id", ""),
            "seq": next(_seq_counter),
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "research_topic": research_topic,
            "tool": tool,
            "attempt": attempt,
            "duration_ms": duration_ms,
            "status": status,
            "failure_kind": failure_kind,
            "reason": reason,
        }
        if params is not None:
            event["params_sha256"] = hashlib.sha256(
                json.dumps(params, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        if output is not None:
            redacted = _redact(str(output))
            event["output_preview"] = redacted[:200]
            event["output_sha256"] = hashlib.sha256(redacted.encode("utf-8")).hexdigest()

        os.makedirs(trace_dir, exist_ok=True)
        path = os.path.join(trace_dir, "run_trace.jsonl")
        _write_line(path, json.dumps(event, ensure_ascii=False))
    except Exception as e:
        _trace_logger.warning("run_trace写入失败，按fail-open忽略，不影响主流程: %s: %s", type(e).__name__, e)
