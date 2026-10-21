"""最小流式测试 —— 验证能不能在运行过程中，实时捕获到 sql_executor 被调用的事件。
先只做侦察，不写断言，看清楚事件结构长什么样再说。
"""

import asyncio
from langgraph_sdk import get_client

LANGGRAPH_URL = "http://127.0.0.1:2024"
ASSISTANT_ID = "Deep Researcher"


async def run_stream_test():
    client = get_client(url=LANGGRAPH_URL)

    thread = await client.threads.create()
    print(f"已创建对话线程：{thread['thread_id']}")
    print("开始流式接收事件...\n")

    event_count = 0
    async for chunk in client.runs.stream(
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
        stream_mode="updates",
        stream_subgraphs=True,
    ):
        event_count += 1
        event_str = str(chunk)
        # 这次不过滤，把每个事件的前300字符都打出来，看清楚整体流程走了哪些节点
        print(f"--- 第{event_count}个事件 ---")
        print(event_str[:300])
        if "sql_executor" in event_str:
            print(">>> 这个事件包含 sql_executor，完整内容：")
            print(event_str)
        print()

    print(f"\n流式接收结束，一共收到 {event_count} 个事件。")


asyncio.run(run_stream_test())
