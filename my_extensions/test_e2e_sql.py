"""端到端测试 —— 通过 langgraph-sdk 真正提交问题给运行中的服务，
检查 Trace 里是否真的调用了 sql_executor，而不只是看最终答案对不对。

运行前提：langgraph dev 必须正在运行。
"""

import asyncio
from langgraph_sdk import get_client

# 如果启动时端口不是这个，改成你终端里实际显示的端口号
LANGGRAPH_URL = "http://127.0.0.1:2024"
ASSISTANT_ID = "Deep Researcher"


async def run_e2e_test():
    client = get_client(url=LANGGRAPH_URL)

    thread = await client.threads.create()
    print(f"已创建对话线程：{thread['thread_id']}")

    run = await client.runs.create(
        thread_id=thread["thread_id"],
        assistant_id=ASSISTANT_ID,
        input={
            "messages": [{
                "role": "human",
                "content": (
                    "我们公司内部数据库里，融资金额最高的三家公司是谁？"
                    "不限行业、地区和融资轮次，直接查全部数据。"
                )
            }]
        },
    )
    print(f"已提交任务，等待运行完成...")

    await client.runs.join(thread_id=thread["thread_id"], run_id=run["run_id"])
    print("运行完成，正在检查结果...\n")

    state = await client.threads.get_state(thread["thread_id"])
    all_messages = state["values"]

    full_text = str(all_messages)

    print("=== 调试：打印完整返回内容，先看看这次运行到底发生了什么 ===")
    print(full_text[:3000])
    print("...(内容过长，只显示前3000字符)...")
    print()

    print("=== 检查1：Trace 里是否出现 sql_executor 工具调用 ===")
    assert "sql_executor" in full_text, "❌ 没有找到 sql_executor 被调用的痕迹！"
    print("✅ 确认 sql_executor 被调用了")

    print("\n=== 检查2：工具返回结果里是否包含正确的数据 ===")
    assert "SynthMotion" in full_text, "❌ 没有找到预期的公司名 SynthMotion"
    assert "80000000" in full_text or "80,000,000" in full_text, "❌ 没有找到预期的金额"
    print("✅ 确认查询结果包含正确的公司和金额")

    print("\n=== 检查3：最终报告是否也提到了正确答案 ===")
    final_report = all_messages.get("final_report", "")
    assert "SynthMotion" in final_report, "❌ 最终报告里没有提到正确答案"
    print("✅ 最终报告与工具查询结果一致")

    print("\n全部端到端检查通过！")


asyncio.run(run_e2e_test())
