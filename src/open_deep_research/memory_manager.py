"""memory_manager.py —— 跨会话记忆的读写封装。

设计原则（跟 sql_executor/internal_doc_search 保持一致的独立可测试性）：
- 不直接嵌入图节点内部写死逻辑，单独封装成两个函数，方便脱离图独立测试
- user_id 用于隔离不同用户的记忆，不是可选参数
- 读写失败不应该让整个研究流程崩溃——记忆是辅助能力，不是核心链路，
  失败时应该降级（不影响主流程），不能因为记忆模块出问题导致整个任务失败
"""

import os
from dotenv import load_dotenv
load_dotenv()

from mem0 import Memory

from open_deep_research.memory_guard import admit_write, filter_read, guard_enabled

_config = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": "gpt-4o-mini",
            "temperature": 0.1,
        },
    },
}

_memory_instance = None

# 演示范围声明：这是一个固定的、单用户的演示身份，不是真正的多用户
# 隔离机制。真实系统应该由认证层提供稳定的用户ID，再传给这里——
# 当前图（deep_researcher.py）里没有真实的用户身份概念，这是一个
# 刻意的简化，用来演示"同一用户跨对话复用记忆"这个机制本身，
# 不能当作多用户隔离已经实现的证据（多用户隔离在 test_memory_manager.py
# 里用两个不同的 user_id 单独测试过，那是验证隔离机制本身有效，
# 跟这里图里接入时用固定ID是两件不同的事）。
DEMO_USER_ID = "demo_user"


def _get_memory() -> Memory:
    """懒加载单例，避免每次调用都重新初始化 Memory 对象（涉及连接本地向量库）。"""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = Memory.from_config(_config)
    return _memory_instance


async def save_memory(text: str, user_id: str) -> dict:
    """存一条记忆。失败时不抛异常，返回一个标记失败的字典，让调用方决定
    要不要继续主流程（设计原则：记忆写入失败不应该阻塞研究任务本身）。
    """
    import asyncio

    if guard_enabled():
        allowed, reason = admit_write(text)
        if not allowed:
            # 被准入过滤拒绝：不是故障，是策略；调用方照常继续主流程
            return {"success": False, "skipped": True, "reason": reason}

    def _run():
        m = _get_memory()
        return m.add(text, user_id=user_id)

    try:
        result = await asyncio.to_thread(_run)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}


async def search_memory(query: str, user_id: str, limit: int = 3) -> dict:
    """检索跟当前问题相关的历史记忆。失败时返回空列表 + 失败标记，
    不阻塞主流程——检索不到记忆，应该表现得跟"这个用户没有历史记忆"一样，
    而不是让整个任务报错。
    """
    import asyncio

    def _run():
        m = _get_memory()
        return m.search(query, filters={"user_id": user_id}, limit=limit)

    try:
        result = await asyncio.to_thread(_run)
        memories = result.get("results", []) if isinstance(result, dict) else result
        if guard_enabled():
            memories, dropped = filter_read(memories)
            return {"success": True, "memories": memories, "dropped": dropped}
        return {"success": True, "memories": memories}
    except Exception as e:
        return {"success": False, "memories": [], "error": f"{type(e).__name__}: {str(e)}"}
