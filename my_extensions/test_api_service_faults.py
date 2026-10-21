"""api_service 的 Redis 故障 / 进程重启 离线测试（fakeredis 模拟，不碰真实 Redis、网络与模型）。

重要边界：这里只是“模拟 Redis 连接异常”和“用新 app 实例读取旧进程留下的 Redis 状态”，
不能证明真实网络断连、容器重启或进程崩溃时的行为。
修复前（2026-10-12 早些时候）这组测试曾作为“特征测试”记录了 4 个缺口，见 EVIDENCE.md §22 的“修复前行为”；
本文件现在断言修复后的行为。仍然存在的限制写在 KNOWN_LIMITS 里并在末尾打印，不会被测试悄悄掩盖。
"""

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import fakeredis.aioredis as fr  # noqa: E402
import httpx  # noqa: E402
from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: E402

from open_deep_research.api_service import Settings, create_app  # noqa: E402

KNOWN_LIMITS = [
    "Redis 在重试窗口之外持续不可用时，已算完的结果会丢失（只能在 Redis 恢复后把任务回收为 failed，不会续写结果）。",
    "任务不会因进程重启而续跑；只是不再永远停在 running。续跑需要 checkpointer。",
    "孤儿回收是读时懒回收，只有被查询（状态/事件流/幂等重放）时才触发；回收与“恰好刚完成”之间有极小竞态窗口。",
    "以上全部只在 fakeredis 模拟下验证，没有对真实 Redis、真实网络断连或容器重启做过验证。",
]


class FlakyRedis:
    """包装 FakeRedis：down=True 时所有命令抛 ConnectionError；fail_cmds 里的命令单独失败。"""

    def __init__(self):
        self._r = fr.FakeRedis()
        self.down = False
        self.fail_cmds: set[str] = set()

    def __getattr__(self, name):
        attr = getattr(self._r, name)
        if not callable(attr):
            return attr

        async def call(*a, **k):
            if self.down or name in self.fail_cmds:
                raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
            return await attr(*a, **k)
        return call


def make(redis, runner=None, **kw):
    async def default(q):
        await asyncio.sleep(0.01)
        return f"answer:{q}"
    kw.setdefault("write_retry_delay_seconds", 0.01)
    app = create_app(redis, runner or default, Settings(sse_poll_interval_seconds=0.01, **kw))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t")
    return app, client


async def drain(app):
    while app.state.tasks:
        await asyncio.gather(*list(app.state.tasks), return_exceptions=True)


def status_of(resp):
    return resp.json()


def test_healthz_reports_redis_down_with_503():
    async def go():
        r = FlakyRedis(); app, c = make(r)
        ok = await c.get("/healthz"); assert ok.status_code == 200 and ok.json()["redis"] == "up"
        r.down = True
        bad = await c.get("/healthz")
        assert bad.status_code == 503 and bad.json() == {"status": "degraded", "redis": "down"}
        await c.aclose()
    asyncio.run(go())
    print("✅ test_healthz_reports_redis_down_with_503 通过")


def test_redis_down_returns_503_with_retry_after_and_leaks_nothing():
    async def go():
        r = FlakyRedis(); app, c = make(r)
        r.down = True
        resp = await c.post("/research", json={"question": "q"})
        assert resp.status_code == 503 and resp.headers.get("retry-after") == "5"
        assert resp.json() == {"detail": "storage unavailable, retry later"}
        for leak in ("Traceback", "redis", "Connection refused", "6379", "File \""):
            assert leak not in resp.text, f"响应泄漏了内部信息: {leak}"
        assert not app.state.tasks, "Redis 不可用时不应启动任务"
        await c.aclose()
    asyncio.run(go())
    print("✅ test_redis_down_returns_503_with_retry_after_and_leaks_nothing 通过")


def test_status_query_distinguishes_missing_task_from_storage_down():
    async def go():
        r = FlakyRedis(); app, c = make(r)
        assert (await c.get("/research/nope")).status_code == 404
        t = (await c.post("/research", json={"question": "q"})).json()["task_id"]
        await drain(app)
        r.down = True
        resp = await c.get(f"/research/{t}")
        assert resp.status_code == 503 and "redis" not in resp.text.lower()
        await c.aclose()
    asyncio.run(go())
    print("✅ test_status_query_distinguishes_missing_task_from_storage_down 通过")


def test_transient_redis_outage_during_writeback_is_retried_and_result_kept():
    async def go():
        r = FlakyRedis()

        async def runner(q):
            r.down = True
            asyncio.get_running_loop().call_later(0.015, lambda: setattr(r, "down", False))  # 短暂断开后恢复
            return "final report"
        app, c = make(r, runner, write_retries=5)
        tid = (await c.post("/research", json={"question": "q"})).json()["task_id"]
        await drain(app)
        got = (await c.get(f"/research/{tid}")).json()
        assert got["status"] == "succeeded" and got["result"] == "final report"
        await c.aclose()
    asyncio.run(go())
    print("✅ test_transient_redis_outage_during_writeback_is_retried_and_result_kept 通过")


def test_long_outage_loses_result_but_task_is_later_reclaimed_not_stuck_running():
    async def go():
        r = FlakyRedis()

        async def runner(q):
            r.down = True              # 重试窗口内一直不恢复
            return "final report"
        app, c = make(r, runner, write_retries=2, stale_after_seconds=0.2, heartbeat_seconds=0.05)
        tid = (await c.post("/research", json={"question": "q"})).json()["task_id"]
        await asyncio.gather(*list(app.state.tasks), return_exceptions=True)
        r.down = False                 # Redis 恢复
        first = (await c.get(f"/research/{tid}")).json()
        assert first["status"] == "running" and "result" not in first   # 刚恢复时尚未超过 stale 阈值
        await asyncio.sleep(0.3)
        later = (await c.get(f"/research/{tid}")).json()
        assert later["status"] == "failed" and later["error"].startswith("OrphanedTask") and "result" not in later
        await c.aclose()
    asyncio.run(go())
    print("✅ test_long_outage_loses_result_but_task_is_later_reclaimed_not_stuck_running 通过")


def test_metrics_failure_does_not_flip_succeeded_to_failed():
    async def go():
        r = FlakyRedis(); r.fail_cmds = {"incr"}
        app, c = make(r)
        r.fail_cmds = set()
        tid = (await c.post("/research", json={"question": "q"})).json()["task_id"]
        r.fail_cmds = {"incr"}         # 任务运行期间指标计数器失败
        await drain(app)
        r.fail_cmds = set()
        assert (await c.get(f"/research/{tid}")).json()["status"] == "succeeded"
        await c.aclose()
    asyncio.run(go())
    print("✅ test_metrics_failure_does_not_flip_succeeded_to_failed 通过")


def test_restart_orphan_is_reclaimed_on_status_sse_and_idempotent_replay():
    async def go():
        r = FlakyRedis()
        started = asyncio.Event()

        async def hang(q):
            started.set()
            await asyncio.sleep(3600)
        app1, c1 = make(r, hang)
        tid = (await c1.post("/research", json={"question": "q"}, headers={"Idempotency-Key": "k1"})).json()["task_id"]
        await started.wait()
        old = list(app1.state.tasks)
        for t in old:
            t.cancel()
        await asyncio.gather(*old, return_exceptions=True)
        # 还原成“进程硬崩溃”留下的样子：status=running，且很久没有心跳
        await r._r.hset(f"task:{tid}", mapping={"status": "running", "updated_at": f"{time.time() - 1000:.3f}"})
        await c1.aclose()

        app2, c2 = make(r, None, sse_max_seconds=1.0)         # 全新进程，默认 stale 阈值 120s
        again = await c2.post("/research", json={"question": "q"}, headers={"Idempotency-Key": "k1"})
        assert again.status_code == 202 and again.json()["task_id"] == tid and again.json()["idempotent_replay"] is True
        got = (await c2.get(f"/research/{tid}")).json()
        assert got["status"] == "failed" and got["error"].startswith("OrphanedTask")
        ev = await c2.get(f"/research/{tid}/events")
        assert '"status": "failed"' in ev.text and "event: timeout" not in ev.text
        fresh = await c2.post("/research", json={"question": "q"}, headers={"Idempotency-Key": "k2"})
        assert fresh.json()["task_id"] != tid
        await drain(app2)
        assert (await c2.get(f"/research/{fresh.json()['task_id']}")).json()["status"] == "succeeded"
        await c2.aclose()
    asyncio.run(go())
    print("✅ test_restart_orphan_is_reclaimed_on_status_sse_and_idempotent_replay 通过")


def test_recent_running_task_from_another_instance_is_not_reclaimed():
    async def go():
        r = FlakyRedis(); app, c = make(r)
        await r._r.hset("task:other", mapping={"status": "running", "updated_at": f"{time.time():.3f}"})
        assert (await c.get("/research/other")).json()["status"] == "running"
        await c.aclose()
    asyncio.run(go())
    print("✅ test_recent_running_task_from_another_instance_is_not_reclaimed 通过")


def test_live_local_task_is_heartbeated_and_never_reclaimed():
    async def go():
        r = FlakyRedis()

        async def slow(q):
            await asyncio.sleep(0.4)
            return "done"
        app, c = make(r, slow, stale_after_seconds=0.15, heartbeat_seconds=0.03)
        tid = (await c.post("/research", json={"question": "q"})).json()["task_id"]
        await asyncio.sleep(0.05)
        t1 = float((await c.get(f"/research/{tid}")).json()["updated_at"])
        await asyncio.sleep(0.25)                              # 已超过 stale 阈值，但心跳持续刷新
        mid = (await c.get(f"/research/{tid}")).json()
        assert mid["status"] == "running" and float(mid["updated_at"]) > t1
        await drain(app)
        assert (await c.get(f"/research/{tid}")).json()["status"] == "succeeded"
        await c.aclose()
    asyncio.run(go())
    print("✅ test_live_local_task_is_heartbeated_and_never_reclaimed 通过")


def test_runner_failure_while_redis_healthy_is_recorded_as_failed_not_running():
    async def go():
        r = FlakyRedis()

        async def boom(q):
            raise RuntimeError("model exploded")
        app, c = make(r, boom)
        tid = (await c.post("/research", json={"question": "q"})).json()["task_id"]
        await drain(app)
        got = (await c.get(f"/research/{tid}")).json()
        assert got["status"] == "failed" and got["error"].startswith("RuntimeError")
        await c.aclose()
    asyncio.run(go())
    print("✅ test_runner_failure_while_redis_healthy_is_recorded_as_failed_not_running 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("\n全部通过。仍然存在的限制（见 EVIDENCE.md §22）：")
    for g in KNOWN_LIMITS:
        print("  -", g)
