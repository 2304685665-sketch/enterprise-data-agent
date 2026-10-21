"""pgvector 向量库后端（PostgreSQL + pgvector 扩展），接口与 Chroma collection / FaissCollection 对齐：
`count()` / `query(query_texts, n_results)` 返回 Chroma 形状的结果（ids/documents/metadatas/distances），
所以 internal_doc_search 的其余逻辑（混合检索、重排、格式化）一行不改。

启用：ODR_VECTOR_BACKEND=pgvector；连接串 ODR_PG_DSN（默认 postgresql://postgres:postgres@localhost:5433/odr）；
表名 ODR_PG_TABLE（默认 kb_chunks）。依赖 `psycopg[binary]`（可选依赖，见 requirements-pgvector.txt）。
索引由 my_extensions/build_pgvector_index.py 构建。

与 Faiss 后端的区别（诚实）：
- pgvector 的价值不在“更快”（46 个 chunk 上都是毫秒级），而在于向量和结构化数据在同一个库里：
  `query(..., doc_ids=[...])` 用 SQL 的 WHERE 做元数据过滤再排序，Faiss 的 IndexFlatIP 做不到。
- 表上建了 HNSW 索引（vector_cosine_ops）。但行数很少时 PostgreSQL 的优化器会选顺序扫描，所以在本规模下
  实际走的是精确检索；行数大到走 HNSW 时结果是近似的（可用 hnsw.ef_search 调召回），那种规模下没有测过。
- 排序：cosine 距离 `<=>` 升序，并列按写入顺序（ord 列），与纯 Python / numpy 稳定排序一致。
"""
from __future__ import annotations

import json
import os
from typing import Callable, Sequence

from open_deep_research.faiss_store import openai_embed, EMBED_MODEL  # noqa: F401  复用同一个 embedding 函数

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5433/odr"
EmbedFn = Callable[[Sequence[str]], "list[list[float]]"]


def dsn() -> str:
    return os.environ.get("ODR_PG_DSN", DEFAULT_DSN)


def table() -> str:
    t = os.environ.get("ODR_PG_TABLE", "kb_chunks")
    if not t.replace("_", "").isalnum():
        raise ValueError(f"非法表名: {t!r}")
    return t


def _lit(vec) -> str:
    return "[" + ",".join(f"{float(x):.8g}" for x in vec) + "]"


def _connect(connection_dsn: str | None = None):
    import psycopg
    return psycopg.connect(connection_dsn or dsn(), autocommit=True)


def build_index(ids: list[str], documents: list[str], metadatas: list[dict], embed: EmbedFn = openai_embed,
                connection_dsn: str | None = None, tbl: str | None = None) -> int:
    """（重新）建表并写入全部 chunk；幂等：已存在的同名表会被替换。"""
    tbl = tbl or table()
    vecs = embed(documents)
    dim = len(vecs[0])
    with _connect(connection_dsn) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.execute(f"""CREATE TABLE {tbl} (
            ord int PRIMARY KEY, id text UNIQUE NOT NULL, doc_id text, section text,
            content text NOT NULL, meta jsonb NOT NULL, embedding vector({dim}) NOT NULL)""")
        with conn.cursor() as cur:
            for i, (cid, doc, meta, v) in enumerate(zip(ids, documents, metadatas, vecs)):
                cur.execute(f"INSERT INTO {tbl} (ord, id, doc_id, section, content, meta, embedding) "
                            f"VALUES (%s,%s,%s,%s,%s,%s,%s::vector)",
                            (i, cid, meta.get("doc_id"), meta.get("section"), doc, json.dumps(meta, ensure_ascii=False), _lit(v)))
        if dim <= 2000:  # HNSW 的维度上限
            conn.execute(f"CREATE INDEX ON {tbl} USING hnsw (embedding vector_cosine_ops)")
        conn.execute(f"ANALYZE {tbl}")
    return len(ids)


class PgVectorCollection:
    def __init__(self, connection_dsn: str | None = None, tbl: str | None = None, embed: EmbedFn = openai_embed):
        self._dsn, self._tbl, self._embed = connection_dsn or dsn(), tbl or table(), embed

    def count(self) -> int:
        with _connect(self._dsn) as conn:
            return conn.execute(f"SELECT count(*) FROM {self._tbl}").fetchone()[0]

    def query(self, query_texts: Sequence[str], n_results: int = 3, doc_ids: Sequence[str] | None = None) -> dict:
        out = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        qv = self._embed(list(query_texts))
        where, extra = "", []
        if doc_ids:
            where, extra = "WHERE doc_id = ANY(%s)", [list(doc_ids)]
        sql = (f"SELECT id, content, meta, embedding <=> %s::vector AS dist FROM {self._tbl} {where} "
               f"ORDER BY dist, ord LIMIT %s")
        with _connect(self._dsn) as conn:
            for v in qv:
                rows = conn.execute(sql, [_lit(v), *extra, n_results]).fetchall()
                out["ids"].append([r[0] for r in rows])
                out["documents"].append([r[1] for r in rows])
                out["metadatas"].append([r[2] for r in rows])
                out["distances"].append([float(r[3]) for r in rows])
        return out
