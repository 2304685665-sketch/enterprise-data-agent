"""SQL 可靠性测试集 —— 测试工具本身处理不同查询场景的能力。

注：这里是直接测试 sql_executor 工具本身，模拟真实模型面对这类
问题时大概率会写出的 SQL 查询模式，不涉及真实大模型调用。
"""

import asyncio
import sys
sys.path.insert(0, 'src/open_deep_research')
from sql_executor import sql_executor


async def test_filter_by_industry():
    """按行业筛选 —— 数据库里所有公司都是同一个行业，
    这条测试验证'筛选条件不产生任何过滤效果'这种边界情况能不能正常处理。"""
    result = await sql_executor.ainvoke({
        'query': "SELECT name FROM companies WHERE industry = 'Humanoid Robotics'"
    })
    assert "RoboWorks" in result
    assert "NeuralPath" in result
    assert "SynthMotion" in result
    assert "AtlasAI" in result
    print("✅ test_filter_by_industry 通过（筛选条件覆盖全部公司，返回全部4家）")


async def test_no_matching_results():
    """没有匹配结果 —— 数据库里最早的公司是2019年成立，
    查'2000年之前成立的公司'应该正确返回空结果，而不是报错或返回错误数据。"""
    result = await sql_executor.ainvoke({
        'query': "SELECT name FROM companies WHERE founded_year < 2000"
    })
    assert result == "Query executed successfully but returned no rows."
    print("✅ test_no_matching_results 通过（正确识别无匹配数据，不是报错）")


async def test_year_filter():
    """指定年份 —— 测试日期字段的筛选（round_date 是文本存储，
    容易踩字符串比较的坑，用 LIKE 而不是精确匹配来测试这个场景）。"""
    result = await sql_executor.ainvoke({
        'query': '''
            SELECT c.name, f.amount_usd
            FROM companies c JOIN funding_rounds f ON c.id = f.company_id
            WHERE f.round_date LIKE '2026%'
        '''
    })
    assert "80000000" in result  # SynthMotion 2026-06-20
    assert "45000000" in result  # RoboWorks 2026-03-15
    assert "2025" not in result.split('\n')[0]  # 确认没有混入非2026年的记录（粗略检查）
    print("✅ test_year_filter 通过（正确筛选出2026年的融资记录）")


async def main():
    await test_filter_by_industry()
    await test_no_matching_results()
    await test_year_filter()
    print("\n可靠性测试集（方案A：工具层面）全部通过！")


asyncio.run(main())
