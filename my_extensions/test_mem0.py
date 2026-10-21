import os
from dotenv import load_dotenv
load_dotenv()

from mem0 import Memory

# 显式指定 llm 用的模型，避开 gpt-5-mini 默认值和 temperature 参数不兼容的问题
config = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": "gpt-4o-mini",
            "temperature": 0.1,
        },
    },
}

m = Memory.from_config(config)

user_id = "ray_test"

print("=== 第1步：存一条记忆 ===")
result = m.add(
    "用户提到的'XYZ指数'专门指公司内部自定义的一个人形机器人产业景气度指标",
    user_id=user_id
)
print(result)

print("\n=== 第2步：模拟新的一轮对话，检索相关记忆 ===")
related = m.search("XYZ指数是什么意思", filters={"user_id": user_id})
print(related)
