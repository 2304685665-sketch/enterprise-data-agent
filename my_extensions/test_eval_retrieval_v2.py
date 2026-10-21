"""eval_retrieval_v2 的离线测试：假 embedding（确定性哈希向量），不调用任何 API。"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_retrieval as ER
import eval_retrieval_v2 as V2


def fake_embed(texts):
    vs = []
    for t in texts:
        h = hashlib.sha256(t.encode()).digest()
        vs.append([b / 255 - 0.5 for b in h[:16]])
    return vs, sum(len(t) for t in texts) // 2


def test_frozen_query_files_match_pinned_hashes():
    V2.check_query_files()


def test_v2_text_has_title_and_original_text_and_ids_unchanged():
    ch = ER.load_kb_chunks(); t = V2.doc_titles()
    c2 = V2.v2_chunks(ch, t)
    assert [c["id"] for c in ch] == [c["id"] for c in c2]
    assert all(c2[i]["text"].endswith(ch[i]["text"]) and c2[i]["text"].startswith("文档：") for i in range(len(ch)))
    assert "SynthMotion" in c2[0]["text"] and "内部会议纪要" in [c for c in c2 if c["doc_id"].startswith("doc10")][0]["text"]
    assert ch[0]["text"] == ER.load_kb_chunks()[0]["text"], "不得修改原 chunk"


def test_cosine_rank_tie_break_is_original_order_and_deterministic():
    vecs = {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.0, 1.0]}
    assert V2.cosine_rank(vecs, [1.0, 0.0], ["a", "b", "c"]) == ["a", "b", "c"]
    assert V2.cosine_rank(vecs, [1.0, 0.0], ["b", "a", "c"]) == ["b", "a", "c"]
    assert V2.cosine_rank(vecs, [0.0, 1.0], ["a", "b", "c"])[0] == "c"


def test_pipeline_runs_offline_and_reports_all_cells():
    ch = ER.load_kb_chunks(); t = V2.doc_titles()
    v1q = ER.load_queries(V2.V1_QUERIES); held = ER.load_queries(V2.HELDOUT_QUERIES)
    rep = V2.run_experiment(fake_embed, ch, t, v1q, held)
    for idx in ("index_v1", "index_v2"):
        for q in ("v1_queries", "heldout"):
            for m in V2.MODES:
                assert f"{idx}|{q}|{m}" in rep["results"]
    assert rep["results"]["index_v1|heldout|bm25"]["overall"]["n"] == 8
    assert rep["results"]["index_v1|heldout_without_H08|bm25"]["overall"]["n"] == 7
    assert rep["results"]["index_v1|v1_queries|bm25"]["overall"]["n"] == 27
    assert "heldout|vector" in rep["paired"] and "ranks_v1_v2" in rep["paired"]["heldout|vector"]
    assert rep["results"]["index_v2|heldout|hybrid"]["target_coverage"]["n_queries"] == 3   # H03/H06/H07


def test_bm25_v1_index_matches_recorded_result():
    rec = os.path.join(V2.HERE, "eval_results", "retrieval_bm25_v1_20261012.json")
    if not os.path.exists(rec):
        print("   (跳过：没有已记录的 BM25 结果文件)"); return
    ch = ER.load_kb_chunks(); v1q = ER.load_queries(V2.V1_QUERIES)
    got = ER.evaluate(ER.bm25_rankings(v1q, ch), v1q, ch)["overall"]
    old = json.load(open(rec, encoding="utf-8"))["overall"]
    assert abs(got["hit1"] - old["hit1"]) < 1e-9 and abs(got["mrr"] - old["mrr"]) < 1e-9


def test_cost_estimate_is_conservative_and_tiny():
    ch = ER.load_kb_chunks(); t = V2.doc_titles()
    texts = [c["text"] for c in ch] + [c["text"] for c in V2.v2_chunks(ch, t)] + [q["query"] for q in ER.load_queries(V2.V1_QUERIES) + ER.load_queries(V2.HELDOUT_QUERIES)]
    assert 0 < V2.estimate_cost_usd(texts) < 0.01


def test_refuses_without_api_flags_and_does_not_overwrite():
    saved = {k: os.environ.pop(k, None) for k in ("EVAL_RETRIEVAL_ALLOW_API",)}
    try:
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            try:
                V2.main(["--out", out]); assert False
            except SystemExit as e:
                assert "--allow-api" in str(e)
            open(out, "w").write("x")
            try:
                V2.main(["--out", out, "--allow-api"]); assert False
            except SystemExit as e:
                assert "已存在" in str(e)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_refuses_fake_key_and_tiny_budget():
    os.environ["EVAL_RETRIEVAL_ALLOW_API"] = "1"; old = os.environ.get("OPENAI_API_KEY"); os.environ["OPENAI_API_KEY"] = "sk-fake-x"
    try:
        with tempfile.TemporaryDirectory() as d:
            for extra, expect in (([], "假 key"), (["--budget-usd", "0.000001"], "超过预算")):
                try:
                    if extra:
                        os.environ["OPENAI_API_KEY"] = "sk-real-looking"
                    V2.main(["--out", os.path.join(d, "o.json"), "--allow-api"] + extra); assert False
                except SystemExit as e:
                    assert expect in str(e), str(e)
    finally:
        os.environ.pop("EVAL_RETRIEVAL_ALLOW_API", None)
        if old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v(); n += 1; print("✅", k, "通过")
    print(f"全部 {n} 个测试通过")
