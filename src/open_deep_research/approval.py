"""高风险工具调用的人工审批：策略（哪些要审批）+ 审批代理（谁来批、怎么等）。

为什么不用 LangGraph 的 interrupt()：researcher 子图是在 supervisor_tools 里用 asyncio.gather
并行、以程序方式 ainvoke 的。interrupt 恢复时会重新执行整个节点，也就是把同一批 researcher 全部重跑，
已经执行过的工具调用会重复、成本翻倍。这里改为"工具真正执行前 await 一个审批决定"：
只暂停这一次调用，并行的其他 researcher 不受影响，也不需要检查点。

代价（诚实声明）：等待审批期间这次工具调用（以及它所在的任务）占着一个并发位；审批请求
存在代理里，进程重启会丢失（RedisApprovalBroker 把请求放进 Redis，重启后请求仍在，但等待它的
协程已经不在了，超时后调用方会按"未批准"处理）。审批不替代底层限制：批准后执行器自身的只读/沙箱/
超时限制依旧生效，不能因为"人批了"就绕过它们。

策略（可调整，见 classify）：
  python_executor           -> require
  sql_executor 引用 >=2 张表 -> require（单表查询、查 sqlite_master 查看结构 -> auto）
  其他                       -> auto
写操作 SQL 不在这里"批准后执行"：sql_executor 本身就拒绝所有写操作，审批不会改变这一点。
默认关闭：配置 human_approval_enabled=True 且设置了代理才生效。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol

AUTO, REQUIRE = "auto", "require"
APPROVED, DENIED, TIMEOUT = "approved", "denied", "timeout"

_TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", re.I)


_FROM_CLAUSE_RE = re.compile(
    r"\bfrom\s+(.+?)(?=\bwhere\b|\bgroup\b|\border\b|\blimit\b|\bhaving\b|\bunion\b|\bon\b|\bjoin\b|\)|;|$)",
    re.I | re.S)


def referenced_tables(query: str) -> set[str]:
    # 去掉字符串字面量与注释，避免 'from x' 之类的文本误判；去掉标识符引号；
    # 识别 JOIN 的表，以及 FROM a, b 这种逗号式隐式连接。仍是正则，不是 SQL 解析器。
    q = re.sub(r"'(?:[^']|'')*'", "''", query or "")
    q = re.sub(r"--[^\n]*|/\*.*?\*/", " ", q, flags=re.S)
    q = q.replace('"', "").replace("`", "")
    tables = {t.lower() for t in _TABLE_RE.findall(q)}
    for clause in _FROM_CLAUSE_RE.findall(q):
        for item in clause.split(","):
            m = re.match(r"\s*([a-zA-Z_][\w]*)", item)
            if m:
                tables.add(m.group(1).lower())
    return tables


def classify(tool_name: str, args: dict) -> str:
    if tool_name == "python_executor":
        return REQUIRE
    if tool_name == "sql_executor":
        tables = referenced_tables(str((args or {}).get("query", "")))
        tables.discard("sqlite_master")
        return REQUIRE if len(tables) >= 2 else AUTO
    return AUTO


def summarize_call(tool_name: str, args: dict, limit: int = 300) -> str:
    text = json.dumps(args, ensure_ascii=False, default=str)
    return f"{tool_name}: {text[:limit]}"


class ApprovalBroker(Protocol):
    async def request(self, summary: str, timeout: float) -> str: ...


@dataclass
class _Pending:
    summary: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class InMemoryApprovalBroker:
    """进程内代理：request() 登记请求并等待 decide()；用于测试和单进程部署。"""

    def __init__(self):
        self.pending: dict[str, _Pending] = {}

    async def request(self, summary: str, timeout: float) -> str:
        rid = uuid.uuid4().hex
        p = _Pending(summary)
        self.pending[rid] = p
        try:
            await asyncio.wait_for(p.event.wait(), timeout=timeout)
            return p.decision or DENIED
        except asyncio.TimeoutError:
            return TIMEOUT
        finally:
            self.pending.pop(rid, None)

    def list_pending(self) -> list[dict]:
        return [{"id": k, "summary": v.summary, "created_at": v.created_at} for k, v in self.pending.items()]

    def decide(self, rid: str, approve: bool) -> bool:
        p = self.pending.get(rid)
        if p is None or p.event.is_set():
            return False
        p.decision = APPROVED if approve else DENIED
        p.event.set()
        return True


class RedisApprovalBroker:
    """Redis 代理：请求写入 approval:{id}（带 TTL），request() 轮询其 decision 字段。
    审批方通过 decide() 写入决定；只有第一次决定生效（HSETNX）。"""

    def __init__(self, redis, poll_interval: float = 0.2, ttl_seconds: int = 3600):
        self.redis, self.poll, self.ttl = redis, poll_interval, ttl_seconds

    async def request(self, summary: str, timeout: float) -> str:
        rid = uuid.uuid4().hex
        key = f"approval:{rid}"
        await self.redis.hset(key, mapping={"summary": summary, "created_at": f"{time.time():.3f}"})
        await self.redis.expire(key, self.ttl)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                d = await self.redis.hget(key, "decision")
                if d:
                    return d.decode() if isinstance(d, bytes) else d
                await asyncio.sleep(self.poll)
            return TIMEOUT
        finally:
            # 超时/取消后做上标记，防止迟到的批准被误认为生效
            await self.redis.hsetnx(key, "decision", TIMEOUT)

    async def list_pending(self) -> list[dict]:
        out = []
        async for key in self.redis.scan_iter("approval:*"):
            h = await self.redis.hgetall(key)
            h = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in h.items()}
            if "decision" not in h:
                out.append({"id": (key.decode() if isinstance(key, bytes) else key).split(":", 1)[1], **h})
        return out

    async def decide(self, rid: str, approve: bool) -> bool:
        key = f"approval:{rid}"
        if not await self.redis.exists(key):
            return False
        return bool(await self.redis.hsetnx(key, "decision", APPROVED if approve else DENIED))


_broker: Optional[ApprovalBroker] = None


def set_broker(broker: Optional[ApprovalBroker]) -> None:
    global _broker
    _broker = broker


def get_broker() -> Optional[ApprovalBroker]:
    return _broker


async def gate(tool_name: str, args: dict, timeout: float) -> tuple[bool, str]:
    """返回 (是否放行, 决定)。不需要审批、或没有设置代理（此时按 fail-closed 处理需审批的调用）：
    - policy=auto -> (True, 'auto')
    - policy=require 且没有代理 -> (False, 'no_broker')   # 没人能批，就不执行
    - 其余按代理返回的决定。"""
    if classify(tool_name, args) == AUTO:
        return True, "auto"
    broker = get_broker()
    if broker is None:
        return False, "no_broker"
    decision = await broker.request(summarize_call(tool_name, args), timeout)
    return decision == APPROVED, decision
