"""用 Faiss 构建向量索引（需要 OPENAI_API_KEY；一次 embeddings 请求，约几千 token）。
用法: python my_extensions/build_faiss_index.py [--kb-dir my_extensions/knowledge_base_v3] [--out my_extensions/faiss_db]
切分规则与评测一致（eval_retrieval.load_kb_chunks）。"""
import argparse, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
import eval_retrieval as ER
from open_deep_research import faiss_store as FS
ap = argparse.ArgumentParser()
ap.add_argument("--kb-dir", default=os.path.join(HERE, "knowledge_base"))
ap.add_argument("--out", default=os.path.join(HERE, "faiss_db"))
a = ap.parse_args()
if os.environ.get("OPENAI_API_KEY", "").startswith("sk-fake"): raise SystemExit("OPENAI_API_KEY 是假 key，请先 unset。")
from dotenv import load_dotenv; load_dotenv()
ch = ER.load_kb_chunks(a.kb_dir)
n = FS.build_index(a.out, [c["id"] for c in ch], [c["text"] for c in ch],
                   [{"source": c["doc_id"] + ".md", "doc_id": c["doc_id"], "section": c["section"]} for c in ch])
print(f"Faiss 索引已构建：{n} 个 chunk -> {a.out}")
