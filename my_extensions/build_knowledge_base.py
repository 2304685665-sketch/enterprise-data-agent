"""把 knowledge_base 目录里的 Markdown 文档切分并写入 Chroma 向量数据库。

设计原则：
- 按段落（## 标题）切分，不是简单按固定字符数切，保留语义完整性
- 每个 chunk 保留 source（文件名）、doc_id 元数据，方便评测时核对出处
- 本地持久化存储，不需要额外起服务，跟 SQLite 的思路一致
"""

import os
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
load_dotenv()

openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=os.environ.get("OPENAI_API_KEY"),
    model_name="text-embedding-3-small",
)

KB_DIR = os.path.join(os.path.dirname(__file__), "knowledge_base")
CHROMA_DIR = os.path.join(os.path.dirname(__file__), "chroma_db")


def split_into_chunks(text: str, doc_id: str) -> list[dict]:
    """按 ## 标题切分成段落级 chunk，保留 doc_id 元数据。"""
    lines = text.split("\n")
    chunks = []
    current_section = []
    current_title = "开头"

    for line in lines:
        if line.startswith("## "):
            if current_section:
                chunks.append({
                    "text": "\n".join(current_section).strip(),
                    "doc_id": doc_id,
                    "section": current_title,
                })
            current_title = line.replace("## ", "").strip()
            current_section = [line]
        else:
            current_section.append(line)

    if current_section:
        chunks.append({
            "text": "\n".join(current_section).strip(),
            "doc_id": doc_id,
            "section": current_title,
        })

    # 只做这一处改动：过滤掉"开头"这个 chunk（第一个 ## 标题之前的内容），
    # 因为它只包含模拟数据声明和文档标题，信息量太稀薄，可能干扰检索排序。
    # 不改其他任何逻辑（不动 Embedding 模型、查询方式、Top-k）。
    chunks = [c for c in chunks if c["section"] != "开头"]

    return [c for c in chunks if c["text"]]


def main():
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    # 如果已经存在同名 collection，先删掉，保证每次跑这个脚本都是从头干净构建
    try:
        client.delete_collection("company_knowledge_base")
    except Exception:
        pass

    collection = client.create_collection(
        "company_knowledge_base",
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=openai_ef,
    )

    all_ids = []
    all_texts = []
    all_metadatas = []

    for filename in sorted(os.listdir(KB_DIR)):
        if not filename.endswith(".md") or filename == "ANSWER_KEY.md":
            continue

        filepath = os.path.join(KB_DIR, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()

        doc_id = filename.replace(".md", "")
        chunks = split_into_chunks(text, doc_id)

        for i, chunk in enumerate(chunks):
            chunk_id = f"{doc_id}_chunk{i}"
            all_ids.append(chunk_id)
            all_texts.append(chunk["text"])
            all_metadatas.append({
                "source": filename,
                "doc_id": doc_id,
                "section": chunk["section"],
            })

    collection.add(
        ids=all_ids,
        documents=all_texts,
        metadatas=all_metadatas,
    )

    print(f"入库完成，共 {len(all_ids)} 个 chunk，来自 {len(set(m['doc_id'] for m in all_metadatas))} 篇文档")
    print("每个 chunk 的 doc_id 和 section：")
    for i, meta in enumerate(all_metadatas):
        print(f"  [{all_ids[i]}] doc={meta['doc_id']} section={meta['section']}")


if __name__ == "__main__":
    main()
