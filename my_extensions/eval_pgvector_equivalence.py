"""pgvector 后端等价性检验（真实 embedding，约 $0.0002）。
问题：把向量库从 Chroma 换成 pgvector 后，检索结果是否一致？
预先约定的判据：对 87 条查询（33 旧 + 10 held-out + 44 新），v1 索引文本、向量检索：
 ① pgvector 的首个命中名次与已记录结果 `retrieval_v3_20261013.json`（index_v1|*|vector，纯 Python 余弦）逐条相同；
 ② 三组 Hit@1/MRR 与记录值相同。不同则如实列出差异条目，不做解释性修饰。
前提: 已有可连接的 PostgreSQL+pgvector（ODR_PG_DSN；docker compose -f deploy/docker-compose.pgvector.yml up -d）。
用法: EVAL_RETRIEVAL_ALLOW_API=1 python my_extensions/eval_pgvector_equivalence.py --allow-api --out PATH"""
import argparse, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
import eval_retrieval as ER, eval_retrieval_v3 as V3
from open_deep_research import pgvector_store as PG
ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); ap.add_argument("--allow-api", action="store_true")
a = ap.parse_args()
ER.check_out_path(a.out, False); V3.check_inputs()
if not (a.allow_api and os.environ.get("EVAL_RETRIEVAL_ALLOW_API") == "1"): raise SystemExit("需要 --allow-api 且 EVAL_RETRIEVAL_ALLOW_API=1")
if ER.fake_key_problem(): raise SystemExit(ER.fake_key_problem())
from dotenv import load_dotenv; load_dotenv()
chunks = ER.load_kb_chunks(V3.KB_V3); groups = V3.load_groups()
rec = json.load(open(os.path.join(HERE, "eval_results", "retrieval_v3_20261013.json"), encoding="utf-8"))["results"]
PG.build_index([c["id"] for c in chunks], [c["text"] for c in chunks],
               [{"doc_id": c["doc_id"], "section": c["section"]} for c in chunks])
# 网络偶发超时（第一次运行在第 N 次逐条 embedding 请求上 ConnectTimeout）：改为一次批量取所有查询向量并带重试；判据不变。
import time
_all_q = [q["query"] for qs in groups.values() for q in qs]
for _try in range(4):
    try:
        _vecs = PG.openai_embed(_all_q); break
    except Exception as e:
        if _try == 3: raise
        print(f"embedding 请求失败，重试 {_try + 1}/3：{type(e).__name__}"); time.sleep(3 * (_try + 1))
_cache = dict(zip(_all_q, _vecs))
col = PG.PgVectorCollection(embed=lambda texts: [_cache[t] for t in texts])
out, diffs = {}, []
for g, qs in groups.items():
    rk = {q["id"]: col.query([q["query"]], n_results=len(chunks))["ids"][0] for q in qs}
    res = ER.evaluate(rk, qs, chunks)
    old = rec[f"index_v1|{g}|vector"]
    o_rank = {x["id"]: x["first_hit_rank"] for x in old["detail"]}
    n_rank = {x["id"]: x["first_hit_rank"] for x in res["detail"]}
    for i in o_rank:
        if o_rank[i] != n_rank[i]: diffs.append({"group": g, "id": i, "recorded_rank": o_rank[i], "pgvector_rank": n_rank[i]})
    out[g] = {"pgvector": res["overall"], "recorded": old["overall"]}
rep = {"identical_first_hit_rank": not diffs, "n_differences": len(diffs), "differences": diffs, "groups": out,
       "note": "embedding 由 OpenAI API 重新生成，同一文本同一模型的向量理论上相同但 API 并非保证逐位确定；若出现差异需逐条看是否并列或接近并列。"}
ER.write_result(a.out, json.dumps(rep, ensure_ascii=False, indent=1))
print(json.dumps({"identical": rep["identical_first_hit_rank"], "n_diff": len(diffs), "groups": out}, ensure_ascii=False, indent=1))
