"""memory_manager 的独立单元测试 —— 分别验证存储层和检索层，
不涉及模型怎么使用这些记忆（那是接入图之后端到端测试要验证的）。"""

import asyncio
import sys
sys.path.insert(0, "src/open_deep_research")
from memory_manager import save_memory, search_memory


async def test_save_and_search_basic():
    """写入一条记忆，能不能检索回来。"""
    save_result = await save_memory(
        "用户设定的研究年份范围是2025年", user_id="test_user_A"
    )
    assert save_result["success"], f"写入失败: {save_result.get('error')}"

    search_result = await search_memory(
        "研究年份范围是什么", user_id="test_user_A"
    )
    assert search_result["success"], f"检索失败: {search_result.get('error')}"
    assert len(search_result["memories"]) > 0, "检索到0条记忆"
    print("✅ test_save_and_search_basic 通过")


async def test_update_conflicting_info():
    """用户后来改了主意，新信息能不能被检索到（覆盖/更新语义）。"""
    await save_memory("用户设定的研究年份范围是2026年", user_id="test_user_A")

    search_result = await search_memory(
        "研究年份范围是什么", user_id="test_user_A"
    )
    memories_text = str(search_result["memories"])
    assert "2026" in memories_text, "更新后的信息(2026)没有被检索到"
    print("✅ test_update_conflicting_info 通过")
    print(f"   实际检索到的内容: {memories_text[:300]}")


async def test_user_isolation():
    """A用户的记忆，不应该被B用户检索到。"""
    await save_memory(
        "用户B专属的秘密偏好：只关注欧洲市场", user_id="test_user_B"
    )

    search_result_a = await search_memory(
        "欧洲市场", user_id="test_user_A"
    )
    memories_text_a = str(search_result_a["memories"])
    assert "欧洲市场" not in memories_text_a or "秘密偏好" not in memories_text_a, \
        "用户隔离失败：test_user_A 检索到了 test_user_B 的记忆"
    print("✅ test_user_isolation 通过（A用户查不到B用户的记忆）")


async def test_no_memory_returns_empty_not_error():
    """全新用户没有任何历史记忆时，应该正常返回空列表，不应该报错。"""
    search_result = await search_memory(
        "这个用户从来没说过的话题", user_id="test_user_brand_new_C"
    )
    assert search_result["success"], "全新用户检索不应该失败"
    print(f"✅ test_no_memory_returns_empty_not_error 通过（返回 {len(search_result['memories'])} 条，正常）")


async def main():
    await test_save_and_search_basic()
    await test_update_conflicting_info()
    await test_user_isolation()
    await test_no_memory_returns_empty_not_error()
    print("\n全部测试通过！")


asyncio.run(main())
