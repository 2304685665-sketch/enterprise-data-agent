"""本地演示：用 fakeredis 和假 runner 启动服务，用来在浏览器里看前端界面（不需要 Redis、不调用任何模型）。

用法：PYTHONPATH=src python my_extensions/demo_service.py   然后打开 http://127.0.0.1:8000
fakeredis 仅用于演示；任务答案是固定文本，"失败"演示：问题以 boom 开头。
包含一个内存审批代理：问题以 approve 开头时会产生一条待审批请求，可在页面上批准/拒绝。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import fakeredis.aioredis as fr  # noqa: E402
import uvicorn  # noqa: E402

from open_deep_research import approval  # noqa: E402
from open_deep_research.api_service import Settings, create_app  # noqa: E402

redis = fr.FakeRedis()
broker = approval.RedisApprovalBroker(redis, poll_interval=0.2)


async def runner(question: str) -> str:
    if question.startswith("approve"):
        decision = await broker.request(f"python_executor: {{'code': 'print({question!r})'}}", timeout=120)
        if decision != approval.APPROVED:
            return f"（演示）审批结果：{decision}，未执行"
    await asyncio.sleep(1.5)
    if question.startswith("boom"):
        raise RuntimeError("演示用的失败")
    return f"（演示）这是对「{question}」的固定回答。"


app = create_app(redis, runner, Settings(rate_limit_per_window=30), approval_broker=broker)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
