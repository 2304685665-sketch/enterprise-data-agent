"""FastAPI + Redis 服务层：异步任务、幂等、限流、状态查询、SSE 事件流。

设计取舍（先读这里）：
- create_app(redis, runner, settings) 全部依赖注入：测试用 fakeredis 与假 runner，不碰真实 Redis/模型。
- 异步任务 = 进程内 asyncio.Task + Redis 里持久化任务状态。**进程重启会丢掉正在跑的任务**，
  任务不会续跑（续跑需要 checkpointer，不在本模块范围内）。为避免状态永远停在 running：执行中每
  heartbeat_seconds 刷新一次 updated_at；pending/running 且超过 stale_after_seconds 没有刷新、并且不是本进程
  正在执行的任务，在被查询（状态/事件流/幂等重放）时标记为 failed（OrphanedTask）。这是读时懒回收，
  不是调度器；多实例共享 Redis 时依赖心跳而非进程内集合，且回收与"恰好刚完成"之间存在极小的竞态窗口。
- Redis 故障：最终状态写回有有限次重试（指数退避）；仍失败时任务停在 running 直到被回收为 failed，
  结果丢失（不会假装成功）。接口遇到 RedisError 返回 503（带 Retry-After），不返回内部细节。指标计数是尽力而为，
  失败不影响任务状态。
- 幂等：Idempotency-Key + 请求体哈希。同一 key 同一请求体返回同一个 task_id，不重复执行；同一 key
  不同请求体返回 409。用 SET NX 抢占，两个并发的相同请求只有一个会真正创建任务。
- 限流：按调用方（X-API-Key，没有则客户端 IP）固定窗口计数。固定窗口在窗口边界处允许最多 2×上限的突发，
  这是已知特性；计数器用 "SET NX EX 再 INCR"，键一定带 TTL，不会因进程在两步之间崩溃而永不过期。
- 认证：设置了 api_keys 才启用（X-API-Key 必须在集合内），没设置则开放——这是演示服务，不是完整的鉴权体系。
- SSE：事件是任务"状态变化"（pending/running/succeeded/failed），不是模型 token 流；以轮询 Redis 实现。
- 错误：任务失败只向客户端暴露异常类型与简短消息，不返回堆栈。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

Runner = Callable[[str], Awaitable[str]]
TERMINAL = ("succeeded", "failed")


@dataclass
class Settings:
    api_keys: frozenset[str] = frozenset()
    rate_limit_per_window: int = 10
    rate_limit_window_seconds: int = 60
    max_concurrent_tasks: int = 2
    task_ttl_seconds: int = 86400
    idempotency_ttl_seconds: int = 86400
    sse_poll_interval_seconds: float = 0.2
    sse_max_seconds: float = 600.0
    max_question_chars: int = 4000
    clock: Callable[[], float] = time.time  # 测试注入；用于按天统计活跃调用方
    duration_samples: int = 200
    # 可靠性：写回状态重试、心跳、孤儿任务回收。stale_after_seconds 必须明显大于 heartbeat_seconds。
    write_retries: int = 3
    write_retry_delay_seconds: float = 0.2
    heartbeat_seconds: float = 30.0
    stale_after_seconds: float = 120.0


class ResearchRequest(BaseModel):
    question: str = Field(..., min_length=1)


class Decision(BaseModel):
    approve: bool


def _body_hash(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def create_app(redis, runner: Runner, settings: Optional[Settings] = None, approval_broker=None) -> FastAPI:
    st = settings or Settings()
    app = FastAPI(title="Deep Research Service")
    sem = asyncio.Semaphore(st.max_concurrent_tasks)
    running: set[asyncio.Task] = set()
    app.state.tasks = running  # 供测试等待收尾

    def _task_key(tid: str) -> str:
        return f"task:{tid}"

    async def _set(tid: str, **fields):
        fields["updated_at"] = f"{time.time():.3f}"
        await redis.hset(_task_key(tid), mapping=fields)
        await redis.expire(_task_key(tid), st.task_ttl_seconds)

    async def _get(tid: str) -> Optional[dict]:
        raw = await redis.hgetall(_task_key(tid))
        if not raw:
            return None
        return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}

    async def _set_retry(tid: str, **fields) -> bool:
        for attempt in range(max(1, st.write_retries)):
            try:
                await _set(tid, **fields)
                return True
            except RedisError as e:
                logger.warning("task %s state write failed (attempt %d): %s", tid, attempt + 1, type(e).__name__)
                if attempt + 1 < st.write_retries:
                    await asyncio.sleep(st.write_retry_delay_seconds * (2 ** attempt))
        logger.error("task %s: giving up writing %s after %d attempts", tid, fields.get("status"), st.write_retries)
        return False

    async def _incr(name: str):
        try:
            await redis.incr(f"metrics:{name}")
        except RedisError:  # 指标尽力而为，不影响任务状态
            logger.warning("metrics counter %s not updated", name)

    local_ids: set[str] = set()  # 本进程正在执行的任务 id

    async def _reclaim_if_stale(tid: str, data: Optional[dict]) -> Optional[dict]:
        if not data or data.get("status") not in ("pending", "running") or tid in local_ids:
            return data
        try:
            last = float(data.get("updated_at") or data.get("created_at") or 0)
        except ValueError:
            last = 0.0
        if time.time() - last <= st.stale_after_seconds:
            return data
        latest = await _get(tid)  # 重新读取一次，缩小竞态窗口
        if not latest or latest.get("status") not in ("pending", "running"):
            return latest
        if float(latest.get("updated_at") or latest.get("created_at") or 0) != last:
            return latest
        await _set(tid, status="failed",
                   error="OrphanedTask: no heartbeat from any worker (process restarted or storage was unavailable); result lost")
        await _incr("failed")
        await _incr("orphaned_reclaimed")
        return await _get(tid)

    async def _heartbeat(tid: str):
        while True:
            await asyncio.sleep(st.heartbeat_seconds)
            try:
                await _set(tid, status="running")
            except RedisError:
                logger.warning("task %s heartbeat failed", tid)

    def _day(offset: int = 0) -> str:
        return time.strftime("%Y%m%d", time.gmtime(st.clock() - offset * 86400))

    async def _touch_client(client: str):
        # 只存客户端标识的哈希前缀，绝不把 API key / IP 原文写进 Redis
        h = hashlib.sha256(client.encode("utf-8")).hexdigest()[:16]
        key = f"metrics:clients:{_day()}"
        await redis.sadd(key, h)
        await redis.expire(key, 40 * 86400)

    def _client_id(request: Request, api_key: Optional[str]) -> str:
        return f"key:{api_key}" if api_key else f"ip:{request.client.host if request.client else 'unknown'}"

    async def _authenticate(api_key: Optional[str]):
        if st.api_keys and api_key not in st.api_keys:
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    async def _rate_limit(client: str):
        key = f"rl:{client}"
        await redis.set(key, 0, nx=True, ex=st.rate_limit_window_seconds)
        count = await redis.incr(key)
        if count > st.rate_limit_per_window:
            await _incr("rate_limited")
            ttl = await redis.ttl(key)
            retry = max(int(ttl), 1) if ttl and ttl > 0 else st.rate_limit_window_seconds
            raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": str(retry)})

    async def _record_duration(ms: float):
        await redis.lpush("metrics:durations_ms", f"{ms:.1f}")
        await redis.ltrim("metrics:durations_ms", 0, st.duration_samples - 1)

    async def _execute(tid: str, question: str):
        local_ids.add(tid)
        hb: Optional[asyncio.Task] = None
        try:
            async with sem:
                if not await _set_retry(tid, status="running"):
                    return  # 存储不可用：不执行（避免做了一堆事却无处记录），状态留给回收逻辑
                hb = asyncio.create_task(_heartbeat(tid))
                t0 = time.monotonic()
                try:
                    result = await runner(question)
                    if await _set_retry(tid, status="succeeded", result=result):
                        await _incr("succeeded")
                except asyncio.CancelledError:
                    await _set_retry(tid, status="failed", error="CancelledError: task cancelled")
                    await _incr("failed")
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.exception("task %s failed", tid)
                    if await _set_retry(tid, status="failed", error=f"{type(e).__name__}: {str(e)[:200]}"):
                        await _incr("failed")
                finally:
                    try:
                        await _record_duration((time.monotonic() - t0) * 1000)
                    except RedisError:
                        pass
        finally:
            if hb is not None:
                hb.cancel()
            local_ids.discard(tid)

    @app.exception_handler(RedisError)
    async def _redis_unavailable(request: Request, exc: RedisError):
        logger.error("redis error on %s: %s", request.url.path, type(exc).__name__)
        return JSONResponse({"detail": "storage unavailable, retry later"}, status_code=503, headers={"Retry-After": "5"})

    @app.get("/healthz")
    async def healthz():
        try:
            await redis.ping()
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "degraded", "redis": "down"}, status_code=503)
        return {"status": "ok", "redis": "up"}

    @app.post("/research", status_code=202)
    async def create_research(
        body: ResearchRequest,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    ):
        await _authenticate(x_api_key)
        if len(body.question) > st.max_question_chars:
            raise HTTPException(status_code=422, detail=f"question exceeds {st.max_question_chars} chars")
        await _rate_limit(_client_id(request, x_api_key))
        await _touch_client(_client_id(request, x_api_key))

        new_id = uuid.uuid4().hex
        bh = _body_hash(body.question)
        if idempotency_key:
            idem_key = f"idem:{_client_id(request, x_api_key)}:{idempotency_key}"
            won = await redis.set(idem_key, f"{new_id}:{bh}", nx=True, ex=st.idempotency_ttl_seconds)
            if not won:
                raw = await redis.get(idem_key)
                prior = (raw.decode() if isinstance(raw, bytes) else raw) or ""
                prior_id, _, prior_hash = prior.partition(":")
                if prior_hash != bh:
                    raise HTTPException(status_code=409, detail="Idempotency-Key reused with a different request body")
                await _reclaim_if_stale(prior_id, await _get(prior_id))  # 重放前先回收死任务，客户端会看到 failed
                await _incr("idempotent_replays")
                return JSONResponse({"task_id": prior_id, "status_url": f"/research/{prior_id}", "idempotent_replay": True}, status_code=202)

        await _set(new_id, status="pending", question=body.question, created_at=f"{time.time():.3f}")
        await _incr("created")
        t = asyncio.create_task(_execute(new_id, body.question))
        running.add(t)
        t.add_done_callback(running.discard)
        return {"task_id": new_id, "status_url": f"/research/{new_id}", "idempotent_replay": False}

    @app.get("/research/{task_id}")
    async def get_research(task_id: str, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
        await _authenticate(x_api_key)
        data = await _reclaim_if_stale(task_id, await _get(task_id))
        if data is None:
            raise HTTPException(status_code=404, detail="task not found")
        data.pop("question", None)
        return {"task_id": task_id, **data}

    @app.get("/research/{task_id}/events")
    async def research_events(task_id: str, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
        await _authenticate(x_api_key)
        if await _get(task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def gen():
            last = None
            deadline = time.monotonic() + st.sse_max_seconds
            while time.monotonic() < deadline:
                data = await _reclaim_if_stale(task_id, await _get(task_id))
                status = (data or {}).get("status")
                if status != last:
                    last = status
                    payload = {"task_id": task_id, "status": status}
                    if status == "succeeded":
                        payload["result"] = data.get("result")
                    if status == "failed":
                        payload["error"] = data.get("error")
                    yield f"event: status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if status in TERMINAL:
                    return
                await asyncio.sleep(st.sse_poll_interval_seconds)
            yield f"event: timeout\ndata: {json.dumps({'task_id': task_id})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    def _pct(values: list[float], p: float) -> Optional[float]:
        if not values:
            return None
        v = sorted(values)
        k = (len(v) - 1) * p / 100
        f, c = int(k), min(int(k) + 1, len(v) - 1)
        return v[f] + (v[c] - v[f]) * (k - f)

    @app.get("/metrics/summary")
    async def metrics_summary(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
        await _authenticate(x_api_key)

        async def n(name: str) -> int:
            v = await redis.get(f"metrics:{name}")
            return int(v) if v else 0

        created, ok, bad = await n("created"), await n("succeeded"), await n("failed")
        durs = [float(x) for x in await redis.lrange("metrics:durations_ms", 0, -1)]
        days = [_day(i) for i in range(7)]
        daily = {}
        for d in days:
            daily[d] = await redis.scard(f"metrics:clients:{d}")
        today = await redis.smembers(f"metrics:clients:{days[0]}")
        earlier: set = set()
        for d in days[1:]:
            earlier |= set(await redis.smembers(f"metrics:clients:{d}"))
        return {
            "tasks_created": created,
            "tasks_succeeded": ok,
            "tasks_failed": bad,
            "success_rate": (ok / (ok + bad)) if (ok + bad) else None,
            "idempotent_replays": await n("idempotent_replays"),
            "rate_limited": await n("rate_limited"),
            "duration_ms": {"p50": _pct(durs, 50), "p95": _pct(durs, 95), "samples": len(durs)},
            "daily_active_clients": daily,
            "returning_clients_today": len(set(today) & earlier),
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics_prometheus(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
        """Prometheus 文本格式（0.0.4）。字段与 /metrics/summary 同源（Redis 计数器），不引入 prometheus_client 依赖。
        鉴权与 summary 一致（X-API-Key）；Prometheus 抓取时用 http_headers 带上该头。"""
        await _authenticate(x_api_key)

        async def n(name: str) -> int:
            v = await redis.get(f"metrics:{name}")
            return int(v) if v else 0

        durs = [float(x) for x in await redis.lrange("metrics:durations_ms", 0, -1)]
        lines = []

        def counter(name, help_, value):
            lines.extend([f"# HELP {name} {help_}", f"# TYPE {name} counter", f"{name} {value}"])

        counter("odr_tasks_created_total", "Research tasks accepted.", await n("created"))
        counter("odr_tasks_succeeded_total", "Research tasks that finished successfully.", await n("succeeded"))
        counter("odr_tasks_failed_total", "Research tasks that failed.", await n("failed"))
        counter("odr_idempotent_replays_total", "Requests answered from an idempotency key.", await n("idempotent_replays"))
        counter("odr_rate_limited_total", "Requests rejected by the rate limiter.", await n("rate_limited"))
        lines.extend(["# HELP odr_task_duration_ms Recent task duration quantiles (last N samples).",
                      "# TYPE odr_task_duration_ms summary"])
        for q in (50, 95):
            v = _pct(durs, q)
            if v is not None:
                lines.append(f'odr_task_duration_ms{{quantile="{q / 100}"}} {v}')
        lines.append(f"odr_task_duration_ms_count {len(durs)}")
        return "\n".join(lines) + "\n"

    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(os.path.join(web_dir, "index.html"), media_type="text/html")

    @app.get("/static/app.js", include_in_schema=False)
    async def app_js():
        return FileResponse(os.path.join(web_dir, "app.js"), media_type="application/javascript")

    if approval_broker is not None:
        @app.get("/approvals")
        async def list_approvals(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
            await _authenticate(x_api_key)
            pending = approval_broker.list_pending()
            if asyncio.iscoroutine(pending):
                pending = await pending
            return {"pending": pending}

        @app.post("/approvals/{approval_id}")
        async def decide_approval(approval_id: str, body: Decision,
                                  x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
            await _authenticate(x_api_key)
            ok = approval_broker.decide(approval_id, body.approve)
            if asyncio.iscoroutine(ok):
                ok = await ok
            if not ok:
                raise HTTPException(status_code=404, detail="approval not found or already decided")
            return {"approval_id": approval_id, "decision": "approved" if body.approve else "denied"}

    return app


async def default_runner(question: str) -> str:
    """真实 runner：调用 deep_researcher 图并返回最终报告文本（需要真实模型 key，测试不使用）。"""
    from langchain_core.messages import HumanMessage

    from open_deep_research.deep_researcher import deep_researcher

    out = await deep_researcher.ainvoke({"messages": [HumanMessage(content=question)]})
    return out.get("final_report", "") or ""


def build_default_app() -> FastAPI:  # pragma: no cover - 需要真实 Redis
    import os

    import redis.asyncio as aioredis

    keys = frozenset(k for k in os.environ.get("SERVICE_API_KEYS", "").split(",") if k.strip())
    return create_app(
        aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
        default_runner,
        Settings(api_keys=keys),
    )
