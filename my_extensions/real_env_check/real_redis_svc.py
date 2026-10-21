import asyncio, sys
sys.path.insert(0, "/mnt/user-data/uploads/ai项目/open_deep_research/src")
import redis.asyncio as ar
from open_deep_research.api_service import create_app, Settings
async def runner(q):
    await asyncio.sleep(6)
    return f"answer:{q}"
app = create_app(ar.Redis(host="127.0.0.1", port=6391), runner,
    Settings(heartbeat_seconds=0.5, stale_after_seconds=2.0, write_retry_delay_seconds=0.2, sse_poll_interval_seconds=0.1))
