"""精确检测版 —— 不看文本里有没有出现字符串，直接读 tool_calls 这个结构化字段。"""

import asyncio
from langgraph_sdk import get_client

LANGGRAPH_URL = "http://127.0.0.1:2024"
ASSISTANT_ID = "Deep Researcher"


async def run_test():
    client = get_client(url=LANGGRAPH_URL)
    thread = await client.threads.create()
    print(f"已创建对话线程：{thread['thread_id']}")
    print("开始流式接收事件...\n")

    sql_executor_called = False
    sql_executor_result = None

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
        if not chunk.data:
            continue
        # 遍历这个事件里所有节点的更新内容
        for node_name, node_data in chunk.data.items():
            if not isinstance(node_data, dict):
                continue
            # researcher 节点：模型决定调用哪个工具，记录在 tool_calls 里
            for msg in node_data.get("researcher_messages", []):
                for tc in msg.get("tool_calls", []) if isinstance(msg, dict) else []:
                    if tc.get("name") == "sql_executor":
                        sql_executor_called = True
                        print(f"✅ 发现 sql_executor 被调用，参数：{tc.get('args')}")
            # researcher_tools 节点：工具真正执行完，结果记录在 ToolMessage 里
            for msg in node_data.get("researcher_messages", []):
                if isinstance(msg, dict) and msg.get("name") == "sql_executor":
                    sql_executor_result = msg.get("content")
                    print(f"✅ 发现 sql_executor 的执行结果：{str(sql_executor_result)[:200]}")

    print(f"\n=== 最终判定 ===")
    print(f"sql_executor 是否被调用: {sql_executor_called}")
    if sql_executor_result:
        print(f"sql_executor 返回结果片段: {str(sql_executor_result)[:200]}")


asyncio.run(run_test())
