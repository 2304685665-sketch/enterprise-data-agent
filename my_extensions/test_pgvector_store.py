"""需要真实 PostgreSQL + pgvector：设置 ODR_PG_TEST_DSN（指向一个可丢弃的数据库）才运行，否则整体跳过。"""
import os, sys
import pytest
np = pytest.importorskip("numpy"); pytest.importorskip("psycopg")
DSN = os.environ.get("ODR_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 ODR_PG_TEST_DSN，跳过真实 PostgreSQL 测试")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from open_deep_research import pgvector_store as PG


def fake_embed(texts):
    return [np.random.RandomState(abs(hash(t)) % (2 ** 31)).randn(16).tolist() for t in texts]


def _build(n=20, tbl="t_pgv_test"):
    ids = [f"c{i}" for i in range(n)]; docs = [f"doc number {i} text" for i in range(n)]
    metas = [{"doc_id": f"d{i % 4}", "section": f"s{i}", "source": f"d{i % 4}.md"} for i in range(n)]
    PG.build_index(ids, docs, metas, embed=fake_embed, connection_dsn=DSN, tbl=tbl)
    return ids, docs, metas, PG.PgVectorCollection(DSN, tbl, embed=fake_embed)


def test_matches_numpy_exact_cosine_with_stable_ties():
    ids, docs, metas, col = _build()
    assert col.count() == 20
    q = "doc number 7 text"
    res = col.query([q], n_results=5)
    D = np.array(fake_embed(docs)); D /= np.linalg.norm(D, axis=1, keepdims=True)
    v = np.array(fake_embed([q])[0]); v /= np.linalg.norm(v)
    want = [ids[i] for i in np.argsort(-(D @ v), kind="stable")[:5]]
    assert res["ids"][0] == want and want[0] == "c7"
    assert res["distances"][0][0] == pytest.approx(0.0, abs=1e-5)
    assert res["metadatas"][0][0]["doc_id"] == "d3" and set(res) == {"ids", "documents", "metadatas", "distances"}


def test_n_results_larger_than_count_and_rebuild_is_idempotent():
    _, _, _, col = _build(n=3)
    assert len(col.query(["x"], 10)["ids"][0]) == 3
    _, _, _, col = _build(n=3)          # 再建一次不应出现重复或报错
    assert col.count() == 3


def test_sql_metadata_filter():
    _, _, _, col = _build()
    res = col.query(["doc number 7 text"], n_results=20, doc_ids=["d1"])
    assert res["ids"][0] and all(m["doc_id"] == "d1" for m in res["metadatas"][0])
    assert "c7" not in res["ids"][0]    # c7 属于 d3，被过滤掉


def test_table_name_rejected_if_injection():
    os.environ["ODR_PG_TABLE"] = "x; DROP TABLE y"
    try:
        with pytest.raises(ValueError):
            PG.table()
    finally:
        os.environ.pop("ODR_PG_TABLE")
