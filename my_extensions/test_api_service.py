"""api_service 的离线测试：fakeredis + 假 runner + httpx ASGI 传输，不启动服务器、不碰真实 Redis 与模型。"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import fakeredis.aioredis as fr  # noqa: E402
import httpx  # noqa: E402

from open_deep_research.api_service import Settings, create_app  # noqa: E402


def run(c):
    return asyncio.run(c)


class Env:
    def __init__(self, runner=None, **kw):
        self.redis = fr.FakeRedis()
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0

        async def default(question):
            self.calls.append(question)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                await asyncio.sleep(0.05)
                return f"answer:{question}"
            finally:
                self.inflight -= 1

        self.app = create_app(self.redis, runner or default, Settings(sse_poll_interval_seconds=0.01, **kw))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")

    async def drain(self):
        while self.app.state.tasks:
            await asyncio.gather(*list(self.app.state.tasks), return_exceptions=True)

    async def close(self):
        await self.drain()
        await self.client.aclose()


def with_env(coro_fn, runner=None, **kw):
    async def go():
        env = Env(runner, **kw)
        try:
            return await coro_fn(env)
        finally:
            await env.close()
    return run(go())


def test_task_lifecycle_success_and_status_endpoint():
    async def t(env):
        r = await env.client.post("/research", json={"question": "q1"})
        assert r.status_code == 202 and r.json()["idempotent_replay"] is False
        tid = r.json()["task_id"]
        await env.drain()
        g = (await env.client.get(f"/research/{tid}")).json()
        assert g["status"] == "succeeded" and g["result"] == "answer:q1" and "question" not in g
        assert (await env.client.get("/research/nope")).status_code == 404
    with_env(t)
    print("✅ test_task_lifecycle_success_and_status_endpoint 通过")


def test_runner_failure_sets_failed_without_traceback():
    async def boom(q):
        raise RuntimeError("model exploded " + "x" * 500)

    async def t(env):
        tid = (await env.client.post("/research", json={"question": "q"})).json()["task_id"]
        await env.drain()
        g = (await env.client.get(f"/research/{tid}")).json()
        assert g["status"] == "failed" and g["error"].startswith("RuntimeError: model exploded")
        assert len(g["error"]) <= 230 and "Traceback" not in g["error"]
    with_env(t, runner=boom)
    print("✅ test_runner_failure_sets_failed_without_traceback 通过")


def test_idempotency_same_key_same_body_runs_once():
    async def t(env):
        h = {"Idempotency-Key": "k1"}
        a = (await env.client.post("/research", json={"question": "q"}, headers=h)).json()
        b = (await env.client.post("/research", json={"question": "q"}, headers=h)).json()
        await env.drain()
        assert a["task_id"] == b["task_id"] and b["idempotent_replay"] is True
        assert env.calls == ["q"]
    with_env(t)
    print("✅ test_idempotency_same_key_same_body_runs_once 通过")


def test_idempotency_concurrent_duplicates_create_single_task():
    async def t(env):
        h = {"Idempotency-Key": "race"}
        rs = await asyncio.gather(*[env.client.post("/research", json={"question": "q"}, headers=h) for _ in range(8)])
        await env.drain()
        assert len({r.json()["task_id"] for r in rs}) == 1 and env.calls == ["q"]
        assert sum(1 for r in rs if not r.json()["idempotent_replay"]) == 1
    with_env(t, rate_limit_per_window=100)
    print("✅ test_idempotency_concurrent_duplicates_create_single_task 通过")


def test_idempotency_key_with_different_body_conflicts():
    async def t(env):
        h = {"Idempotency-Key": "k"}
        await env.client.post("/research", json={"question": "a"}, headers=h)
        r = await env.client.post("/research", json={"question": "b"}, headers=h)
        assert r.status_code == 409
        # 不同调用方可以使用相同的 key（按调用方隔离）
        r2 = await env.client.post("/research", json={"question": "b"}, headers={**h, "X-API-Key": "other"})
        assert r2.status_code == 202
    with_env(t)
    print("✅ test_idempotency_key_with_different_body_conflicts 通过")


def test_rate_limit_blocks_then_resets_and_isolates_clients():
    async def t(env):
        codes = [(await env.client.post("/research", json={"question": f"q{i}"})).status_code for i in range(5)]
        assert codes == [202, 202, 202, 429, 429], codes
        r = await env.client.post("/research", json={"question": "x"})
        assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 1
        assert (await env.client.post("/research", json={"question": "o"}, headers={"X-API-Key": "k2"})).status_code == 202
        await asyncio.sleep(1.1)
        assert (await env.client.post("/research", json={"question": "again"})).status_code == 202
    with_env(t, rate_limit_per_window=3, rate_limit_window_seconds=1)
    print("✅ test_rate_limit_blocks_then_resets_and_isolates_clients 通过")


def test_rate_limit_counter_always_has_ttl():
    async def t(env):
        await env.client.post("/research", json={"question": "q"})
        keys = [k async for k in env.redis.scan_iter("rl:*")]
        assert keys
        for k in keys:
            assert await env.redis.ttl(k) > 0, k
    with_env(t)
    print("✅ test_rate_limit_counter_always_has_ttl 通过")


def test_auth_when_keys_configured():
    async def t(env):
        assert (await env.client.post("/research", json={"question": "q"})).status_code == 401
        assert (await env.client.post("/research", json={"question": "q"}, headers={"X-API-Key": "bad"})).status_code == 401
        r = await env.client.post("/research", json={"question": "q"}, headers={"X-API-Key": "good"})
        assert r.status_code == 202
        assert (await env.client.get(f"/research/{r.json()['task_id']}")).status_code == 401
    with_env(t, api_keys=frozenset({"good"}))
    print("✅ test_auth_when_keys_configured 通过")


def test_concurrency_cap_is_enforced():
    async def t(env):
        for i in range(4):
            await env.client.post("/research", json={"question": f"q{i}"})
        await env.drain()
        assert len(env.calls) == 4 and env.max_inflight == 1, env.max_inflight
    with_env(t, max_concurrent_tasks=1, rate_limit_per_window=100)
    print("✅ test_concurrency_cap_is_enforced 通过")


def test_sse_streams_status_transitions_until_terminal():
    async def t(env):
        tid = (await env.client.post("/research", json={"question": "s"})).json()["task_id"]
        events = []
        async with env.client.stream("GET", f"/research/{tid}/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
        statuses = [e["status"] for e in events]
        assert statuses[-1] == "succeeded" and events[-1]["result"] == "answer:s"
        assert statuses == sorted(set(statuses), key=["pending", "running", "succeeded"].index), statuses
    with_env(t)
    print("✅ test_sse_streams_status_transitions_until_terminal 通过")


def test_validation_and_health():
    async def t(env):
        assert (await env.client.post("/research", json={"question": ""})).status_code == 422
        assert (await env.client.post("/research", json={"question": "x" * 11})).status_code == 422
        assert (await env.client.get("/healthz")).json() == {"status": "ok", "redis": "up"}
    with_env(t, max_question_chars=10)
    print("✅ test_validation_and_health 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
