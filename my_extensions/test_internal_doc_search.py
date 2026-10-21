"""internal_doc_search 工具的单元测试。"""

import asyncio
import sys
sys.path.insert(0, "src/open_deep_research")
from internal_doc_search import internal_doc_search


async def test_basic_search_finds_correct_doc():
    """基础检索：SynthMotion 相关问题应该能查到 doc01。"""
    result = await internal_doc_search.ainvoke({"query": "SynthMotion 是做什么的？"})
    assert "doc01_synthmotion_intro" in result
    print("✅ test_basic_search_finds_correct_doc 通过")


async def test_search_returns_multiple_results():
    """默认应该返回3条结果（用 --- 分隔符判断条数）。"""
    result = await internal_doc_search.ainvoke({"query": "RoboWorks 的技术路线是什么？"})
    separator_count = result.count("---")
    assert separator_count == 2  # 3条结果，2个分隔符
    print("✅ test_search_returns_multiple_results 通过（返回3条结果）")


async def test_custom_n_results():
    """n_results 参数应该生效，测试只要1条结果。"""
    result = await internal_doc_search.ainvoke({
        "query": "RoboWorks 的技术路线是什么？",
        "n_results": 1
    })
    assert "---" not in result  # 只有1条结果，不应该有分隔符
    assert "doc03_roboworks_intro" in result
    print("✅ test_custom_n_results 通过（n_results参数生效）")


async def test_result_contains_source_metadata():
    """返回结果应该包含来源信息（doc_id、section），方便核对出处。"""
    result = await internal_doc_search.ainvoke({"query": "AtlasAI 在哪个领域有技术布局？"})
    assert "Source:" in result
    assert "section:" in result
    print("✅ test_result_contains_source_metadata 通过（结果带来源元数据）")


async def main():
    await test_basic_search_finds_correct_doc()
    await test_search_returns_multiple_results()
    await test_custom_n_results()
    await test_result_contains_source_metadata()
    print("\n全部测试通过！")


asyncio.run(main())
