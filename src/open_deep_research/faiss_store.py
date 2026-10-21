"""可替换的向量库后端：Faiss（IndexFlatIP + 归一化向量 = 精确余弦检索），接口与 Chroma collection 的
`count()` / `query(query_texts, n_results)` 对齐，返回 Chroma 形状的结果（ids/documents/metadatas/distances），
这样 internal_doc_search 的其余逻辑（hybrid/rerank/格式化）一行不改。

选择 Faiss 的原因（诚实）：它是进程内库，不需要另起服务，适合 46 块规模；IndexFlatIP 是精确检索，不是近似，
所以与 Chroma(HNSW, cosine) 的差异只可能来自 HNSW 的近似和并列处理，不来自本实现的“召回损失”。
这不是性能优化：在 46 个 chunk 上两者都是毫秒级，没有也不会有可测的速度差。

启用：ODR_VECTOR_BACKEND=faiss（默认 chroma）；需要 `pip install faiss-cpu`（可选依赖，不在 pyproject 里，
避免改动已验证的镜像依赖）。索引由 my_extensions/build_faiss_index.py 构建，落盘为 index.faiss + meta.json。
"""
from __future__ import annotations

import json
import os
from typing import Callable, Sequence

DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "my_extensions", "faiss_db")
EMBED_MODEL = "text-embedding-3-small"

EmbedFn = Callable[[Sequence[str]], "list[list[float]]"]


def openai_embed(texts: Sequence[str]) -> "list[list[float]]":
    import openai
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    r = client.embeddings.create(model=EMBED_MODEL, input=list(texts))
    return [d.embedding for d in sorted(r.data, key=lambda d: d.index)]


def _normalize(mat):
    import numpy as np
    m = np.asarray(mat, dtype="float32")
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def build_index(directory: str, ids: list[str], documents: list[str], metadatas: list[dict], embed: EmbedFn = openai_embed) -> int:
    import faiss
    os.makedirs(directory, exist_ok=True)
    vecs = _normalize(embed(documents))
    index = faiss.IndexFlatIP(vecs.shape[1])
    index.add(vecs)
    faiss.write_index(index, os.path.join(directory, "index.faiss"))
    with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"model": EMBED_MODEL, "ids": ids, "documents": documents, "metadatas": metadatas}, f, ensure_ascii=False)
    return len(ids)


class FaissCollection:
    def __init__(self, directory: str | None = None, embed: EmbedFn = openai_embed):
        import faiss
        directory = directory or os.environ.get("ODR_FAISS_DIR") or DEFAULT_DIR   # ODR_FAISS_DIR：评测用，指向被投毒的副本索引
        self._index = faiss.read_index(os.path.join(directory, "index.faiss"))
        meta = json.load(open(os.path.join(directory, "meta.json"), encoding="utf-8"))
        self._ids, self._docs, self._metas = meta["ids"], meta["documents"], meta["metadatas"]
        self._embed = embed

    def count(self) -> int:
        return len(self._ids)

    def query(self, query_texts: Sequence[str], n_results: int = 3) -> dict:
        n = min(n_results, self.count())
        q = _normalize(self._embed(list(query_texts)))
        sims, idx = self._index.search(q, n)
        out = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        for row_s, row_i in zip(sims, idx):
            keep = [(s, i) for s, i in zip(row_s, row_i) if i >= 0]
            out["ids"].append([self._ids[i] for _, i in keep])
            out["documents"].append([self._docs[i] for _, i in keep])
            out["metadatas"].append([self._metas[i] for _, i in keep])
            out["distances"].append([float(1.0 - s) for s, _ in keep])   # 余弦距离，与 Chroma cosine 空间同义
        return out


def backend() -> str:
    return os.environ.get("ODR_VECTOR_BACKEND", "chroma").strip().lower()
