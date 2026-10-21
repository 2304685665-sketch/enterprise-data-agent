import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_retrieval as ER, eval_retrieval_v3 as V3, eval_retrieval_v4 as V4, eval_retrieval_v2 as V2


def test_frozen_and_targets_exist_and_no_overlap():
    V4.check_inputs()
    chunks = ER.load_kb_chunks(V3.KB_V3)
    have = {(c["doc_id"], c["section"]) for c in chunks}
    g = V4.load_groups()
    assert len(g["new_v4"]) == 60
    for q in g["new_v4"]:
        for t in q["targets"]:
            assert tuple(t) in have, (q["id"], t)
    seen = [q["query"] for qs in g.values() for q in qs]
    assert len(seen) == len(set(seen))


def test_pipeline_runs_with_fake_embed():
    chunks = ER.load_kb_chunks(V3.KB_V3); titles = V2.doc_titles(V3.KB_V3)
    def embed(texts): return [[float((hash(t) >> k) & 1) + 0.1 for k in range(8)] for t in texts], len(texts)
    rep = V3.run_experiment(embed, chunks, titles, V4.load_groups())
    assert "index_v2|new_v4|hybrid" in rep["results"] and "new_v4|vector" in rep["paired"]
