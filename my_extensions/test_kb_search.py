"""独立测试 Chroma 检索效果，不接大模型，先验证语义检索本身靠不靠谱。"""

import os
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
load_dotenv()

openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=os.environ.get("OPENAI_API_KEY"),
    model_name="text-embedding-3-small",
)

CHROMA_DIR = os.path.join(os.path.dirname(__file__), "chroma_db")


def search(query: str, n_results: int = 3):
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = client.get_collection("company_knowledge_base", embedding_function=openai_ef)
    results = collection.query(query_texts=[query], n_results=n_results)

    print(f"\n=== 查询：{query} ===")
    for i in range(len(results["ids"][0])):
        doc_id = results["metadatas"][0][i]["doc_id"]
        section = results["metadatas"][0][i]["section"]
        distance = results["distances"][0][i]
        text_preview = results["documents"][0][i][:80].replace("\n", " ")
        print(f"  [{i+1}] doc={doc_id} section={section} distance={distance:.3f}")
        print(f"      内容预览: {text_preview}...")


if __name__ == "__main__":
    # 对应 ANSWER_KEY.md 里的几道测试题，验证正确的文档有没有排进 Top-k
    search("SynthMotion 是做什么的？")
    search("RoboWorks 的技术路线是什么？")
    search("内部对 SynthMotion 这个标的怎么看？")
    search("知识库里有没有记录XYZ机器人公司的信息？")
