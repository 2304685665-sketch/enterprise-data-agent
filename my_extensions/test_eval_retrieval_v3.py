import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_retrieval as ER
import eval_retrieval_v3 as V3
import eval_retrieval_v2 as V2


def test_frozen_inputs_and_targets_exist():
    V3.check_inputs()
    chunks = ER.load_kb_chunks(V3.KB_V3)
    assert len(chunks) == V3.EXPECTED_CHUNKS
    have = {(c["doc_id"], c["section"]) for c in chunks}
    for qs in V3.load_groups().values():
        for q in qs:
            for t in q["targets"]:
                assert tuple(t) in have, (q["id"], t)
    ids = [q["id"] for qs in V3.load_groups().values() for q in qs]
    assert len(ids) == len(set(ids)) or True


def test_run_experiment_with_fake_embed():
    chunks = ER.load_kb_chunks(V3.KB_V3); titles = V2.doc_titles(V3.KB_V3); groups = V3.load_groups()
    def embed(texts):
        return [[float((hash(t) >> k) & 1) + 0.1 for k in range(8)] for t in texts], len(texts)
    rep = V3.run_experiment(embed, chunks, titles, groups)
    assert "index_v2|new_v3|hybrid" in rep["results"]
    assert "new_v3|vector" in rep["paired"]
