"""把知识库 chunk 写入 PostgreSQL(pgvector)。需要 OPENAI_API_KEY（一次 embeddings 请求）与可连接的 Postgres（ODR_PG_DSN）。
用法: python my_extensions/build_pgvector_index.py [--kb-dir my_extensions/knowledge_base_v3]"""
import argparse, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
import eval_retrieval as ER
from open_deep_research import pgvector_store as PG
ap = argparse.ArgumentParser(); ap.add_argument("--kb-dir", default=os.path.join(HERE, "knowledge_base_v3"))
a = ap.parse_args()
if os.environ.get("OPENAI_API_KEY", "").startswith("sk-fake"): raise SystemExit("OPENAI_API_KEY 是假 key，请先 unset。")
from dotenv import load_dotenv; load_dotenv()
ch = ER.load_kb_chunks(a.kb_dir)
n = PG.build_index([c["id"] for c in ch], [c["text"] for c in ch],
                   [{"source": c["doc_id"] + ".md", "doc_id": c["doc_id"], "section": c["section"]} for c in ch])
print(f"pgvector 索引已写入：{n} 个 chunk -> {PG.dsn()} 表 {PG.table()}")
