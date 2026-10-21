import os, sys
import pytest
np = pytest.importorskip("numpy"); pytest.importorskip("faiss")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from open_deep_research import faiss_store as FS


def fake_embed(texts):
    rng = []
    for t in texts:
        r = np.random.RandomState(abs(hash(t)) % (2 ** 31)); rng.append(r.randn(16).tolist())
    return rng


def test_build_and_query_matches_numpy_cosine(tmp_path):
    ids = [f"c{i}" for i in range(20)]; docs = [f"doc number {i} text" for i in range(20)]
    metas = [{"doc_id": f"d{i}", "section": "s"} for i in range(20)]
    FS.build_index(str(tmp_path), ids, docs, metas, embed=fake_embed)
    col = FS.FaissCollection(str(tmp_path), embed=fake_embed)
    assert col.count() == 20
    q = "doc number 7 text"
    res = col.query([q], n_results=5)
    # 参照实现：numpy 精确余弦
    D = np.array(fake_embed(docs)); D /= np.linalg.norm(D, axis=1, keepdims=True)
    v = np.array(fake_embed([q])[0]); v /= np.linalg.norm(v)
    want = [ids[i] for i in np.argsort(-(D @ v), kind="stable")[:5]]
    assert res["ids"][0] == want and res["ids"][0][0] == "c7"
    assert res["distances"][0][0] == pytest.approx(0.0, abs=1e-5)
    assert set(res) == {"ids", "documents", "metadatas", "distances"}


def test_n_results_larger_than_count(tmp_path):
    FS.build_index(str(tmp_path), ["a", "b"], ["x", "y"], [{}, {}], embed=fake_embed)
    assert len(FS.FaissCollection(str(tmp_path), embed=fake_embed).query(["x"], 10)["ids"][0]) == 2


def test_backend_default_chroma(monkeypatch):
    monkeypatch.delenv("ODR_VECTOR_BACKEND", raising=False)
    assert FS.backend() == "chroma"
    monkeypatch.setenv("ODR_VECTOR_BACKEND", "FAISS")
    assert FS.backend() == "faiss"
