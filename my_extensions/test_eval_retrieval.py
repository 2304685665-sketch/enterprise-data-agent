"""eval_retrieval 的离线测试：指标计算是否正确，以及查询集标注本身是否有效。
不调用任何 API，也不检查任何检索效果。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_retrieval as ev  # noqa: E402

CHUNKS = ev.load_kb_chunks()
QUERIES = ev.load_queries()


def test_real_kb_has_expected_shape():
    assert len(CHUNKS) == 25, len(CHUNKS)
    assert len({c["id"] for c in CHUNKS}) == 25
    assert all(c["section"] != "开头" for c in CHUNKS)
    print("✅ test_real_kb_has_expected_shape 通过（25 个 chunk）")


def test_every_target_exists_in_the_index():
    existing = {(c["doc_id"], c["section"]) for c in CHUNKS}
    for q in QUERIES:
        for d, s in q["targets"]:
            assert (d, s) in existing, f"{q['id']} 的标注目标不在索引里：{d} / {s}"
    print("✅ test_every_target_exists_in_the_index 通过（所有标注目标都能在 25 个 chunk 里找到）")


def test_query_set_structure_and_reasons():
    ids = [q["id"] for q in QUERIES]
    assert len(ids) == len(set(ids)), "查询 id 必须唯一"
    allowed = {"entity", "semantic", "keyword", "no_answer"}
    for q in QUERIES:
        assert q["type"] in allowed, q["id"]
        assert q["query"].strip() and q["reason"].strip(), f"{q['id']} 缺少查询文本或标注理由"
        if q["type"] == "no_answer":
            assert q["targets"] == [], f"{q['id']} 无答案类不应有目标"
        else:
            assert q["targets"], f"{q['id']} 有答案类必须有目标"
    types = {t: sum(1 for q in QUERIES if q["type"] == t) for t in allowed}
    assert all(types[t] >= 5 for t in allowed), types
    print(f"✅ test_query_set_structure_and_reasons 通过（{types}）")


def test_first_hit_rank_and_metrics_hand_computed():
    qs = [
        {"id": "a", "type": "entity", "query": "x", "targets": [["doc01_synthmotion_intro", "融资历程"]], "reason": "r"},
        {"id": "b", "type": "semantic", "query": "x", "targets": [["doc05_industry_report", "地域分布"]], "reason": "r"},
        {"id": "c", "type": "keyword", "query": "x", "targets": [["doc03_roboworks_intro", "技术路线"]], "reason": "r"},
        {"id": "n", "type": "no_answer", "query": "x", "targets": [], "reason": "r"},
    ]
    t = {q["id"]: ev.target_chunk_ids(q, CHUNKS) for q in qs}
    assert all(len(t[k]) == 1 for k in "abc")
    a, b, c = (next(iter(t[k])) for k in "abc")
    other = next(x["id"] for x in CHUNKS if x["id"] not in {a, b, c})
    rankings = {"a": [a, other], "b": [other, b], "c": [other, c.replace("chunk", "chunkX")], "n": [other]}
    res = ev.evaluate(rankings, qs, CHUNKS)
    # a: 第1名 -> rr=1；b: 第2名 -> rr=0.5；c: 未命中 -> 0；无答案不计入
    assert res["overall"]["n"] == 3
    assert abs(res["overall"]["hit1"] - 1 / 3) < 1e-9
    assert abs(res["overall"]["mrr"] - (1 + 0.5 + 0) / 3) < 1e-9
    assert res["by_type"]["entity"] == {"n": 1, "hit1": 1.0, "mrr": 1.0}
    assert res["by_type"]["semantic"]["mrr"] == 0.5
    assert "no_answer" not in res["by_type"]
    print("✅ test_first_hit_rank_and_metrics_hand_computed 通过")


def test_multi_target_query_hits_on_any_target():
    q = {"id": "m", "type": "semantic", "query": "x",
         "targets": [["doc02_synthmotion_product", "技术布局"], ["doc08_meeting_notes_synthmotion", "待观察风险"]],
         "reason": "r"}
    ids = ev.target_chunk_ids(q, CHUNKS)
    assert len(ids) == 2
    assert ev.first_hit_rank(["zzz", sorted(ids)[1], sorted(ids)[0]], ids) == 2
    print("✅ test_multi_target_query_hits_on_any_target 通过")


class _FakeCollection:
    def __init__(self, ids):
        self._ids = ids
        self.calls = []

    def count(self):
        return len(self._ids)

    def query(self, query_texts, n_results):
        self.calls.append((query_texts, n_results))
        return {"ids": [self._ids[:n_results]]}


def test_vector_candidate_pool_is_capped_and_never_exceeds_collection_size():
    small = _FakeCollection([f"c{i}" for i in range(25)])
    assert len(ev.vector_rank_ids(small, "q")) == 25 and small.calls[0][1] == 25
    big = _FakeCollection([f"c{i}" for i in range(80)])
    assert len(ev.vector_rank_ids(big, "q")) == 50 and big.calls[0][1] == 50
    print("✅ test_vector_candidate_pool_is_capped_and_never_exceeds_collection_size 通过")


def test_hybrid_only_reorders_the_vector_candidate_pool():
    q = [{"id": "q1", "type": "keyword", "query": "伺服电机", "targets": [], "reason": "r"}]
    vec_ids = [c["id"] for c in CHUNKS][:10]  # 向量候选池只有前 10 个
    out = ev.hybrid_rankings(q, CHUNKS, {"q1": vec_ids})["q1"]
    assert set(out) == set(vec_ids), "混合排序结果必须正好是向量候选池的重排，不能引入池外 chunk"
    assert len(out) == len(vec_ids)
    print("✅ test_hybrid_only_reorders_the_vector_candidate_pool 通过")


def test_api_modes_refuse_without_explicit_approval():
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "EVAL_RETRIEVAL_ALLOW_API"}
    for mode in ("vector", "hybrid"):
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_retrieval.py"),
                            "--mode", mode], capture_output=True, text=True, env=env, timeout=30)
        assert p.returncode != 0 and "--allow-api" in (p.stderr + p.stdout), (mode, p.stderr[-300:])
        p2 = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_retrieval.py"),
                             "--mode", mode, "--allow-api"], capture_output=True, text=True, env=env, timeout=30)
        assert p2.returncode != 0, "只有 --allow-api 而没有环境变量也必须拒绝"
    print("✅ test_api_modes_refuse_without_explicit_approval 通过（未批准时 vector/hybrid 均拒绝运行）")


def _qs_for_coverage():
    return [
        {"id": "m2", "type": "semantic", "query": "x",
         "targets": [["doc02_synthmotion_product", "技术布局"], ["doc08_meeting_notes_synthmotion", "待观察风险"]], "reason": "r"},
        {"id": "m3", "type": "semantic", "query": "x",
         "targets": [["doc02_synthmotion_product", "技术布局"], ["doc08_meeting_notes_synthmotion", "待观察风险"],
                     ["doc01_synthmotion_intro", "融资历程"]], "reason": "r"},
        {"id": "s1", "type": "entity", "query": "x", "targets": [["doc01_synthmotion_intro", "融资历程"]], "reason": "r"},
        {"id": "n", "type": "no_answer", "query": "x", "targets": [], "reason": "r"},
    ]


def test_target_coverage_counts_distinct_targets_and_excludes_single_and_no_answer():
    qs = _qs_for_coverage()
    a, b = sorted(ev.target_chunk_ids(qs[0], CHUNKS))
    c = next(iter(ev.target_chunk_ids(qs[2], CHUNKS)))
    other = next(x["id"] for x in CHUNKS if x["id"] not in {a, b, c})
    rk = {"m2": [a, a, a, other, b], "m3": [other, a, b, c], "s1": [c], "n": [a]}
    res = ev.target_coverage(rk, qs, CHUNKS, ks=(1, 2, 3, 5))
    ids = [r["id"] for r in res["detail"]]
    assert ids == ["m2", "m3"], ids  # 单目标与 no_answer 不参与
    m2, m3 = res["detail"]
    # m2: 同一目标重复出现不重复计；b 在第 5 名
    assert [m2[f"coverage@{k}"] for k in (1, 2, 3, 5)] == [0.5, 0.5, 0.5, 1.0]
    assert m2["n_targets"] == 2
    # m3: 3 个目标；k=1 无；k=2 得 a；k=3 得 a,b；k=5(超过列表长度) 得全部
    assert [m3[f"coverage@{k}"] for k in (1, 2, 3, 5)] == [0.0, 1 / 3, 2 / 3, 1.0]
    assert abs(res["mean"]["coverage@5"] - 1.0) < 1e-9 and res["n_queries"] == 2
    assert ev.target_coverage({}, qs, CHUNKS)["detail"][0]["coverage@1"] == 0.0  # 缺排名视为空
    assert ev.target_coverage({}, [qs[2], qs[3]], CHUNKS)["mean"]["coverage@1"] is None  # 无多目标查询
    print("✅ test_target_coverage_counts_distinct_targets_and_excludes_single_and_no_answer 通过")


def test_real_multi_target_queries_are_exactly_the_known_five():
    multi = sorted(q["id"] for q in QUERIES if q["type"] != "no_answer" and len(ev.target_chunk_ids(q, CHUNKS)) >= 2)
    assert multi == ["K4", "M4", "S1", "S2", "S7"], multi
    print("✅ test_real_multi_target_queries_are_exactly_the_known_five 通过")


def test_evaluate_output_unchanged_by_new_analysis():
    import copy
    qs = _qs_for_coverage()
    a, b = sorted(ev.target_chunk_ids(qs[0], CHUNKS))
    rk = {"m2": [b, a], "m3": [a], "s1": ["zzz"], "n": [a]}
    before = copy.deepcopy(ev.evaluate(rk, qs, CHUNKS))
    ev.target_coverage(rk, qs, CHUNKS)
    ev.no_answer_records(rk, qs, CHUNKS, "bm25")
    assert ev.evaluate(rk, qs, CHUNKS) == before
    assert before["overall"]["n"] == 3 and "no_answer" not in before["by_type"]
    assert set(before) == {"overall", "by_type", "detail"}
    print("✅ test_evaluate_output_unchanged_by_new_analysis 通过")


def test_no_answer_records_probe_types_and_distractor_flag():
    n4 = next(q for q in QUERIES if q["id"] == "N4")
    dis = next(c["id"] for c in CHUNKS if (c["doc_id"], c["section"]) == ("doc01_synthmotion_intro", "融资历程"))
    other = next(c["id"] for c in CHUNKS if c["id"] != dis)
    rk = {q["id"]: [other] for q in QUERIES}
    rk["N4"] = [other, dis]
    rec = {r["id"]: r for r in ev.no_answer_records(rk, QUERIES, CHUNKS, "bm25", scores={"N4": {other: 1.5}})}
    assert sorted(rec) == ["N1", "N2", "N3", "N4", "N5", "N6"], sorted(rec)
    assert rec["N3"]["analysis_type"] == "index_coverage_probe"
    assert all(rec[k]["analysis_type"] == "no_answer_probe" for k in ("N1", "N2", "N4", "N5", "N6"))
    assert rec["N4"]["has_known_distractor"] and not rec["N4"]["top1_is_known_distractor"]
    assert rec["N4"]["first_distractor_rank"] == 2 and rec["N4"]["top1_score"] == 1.5
    assert rec["N4"]["top1_chunk_id"] == other and rec["N4"]["mode"] == "bm25"
    assert not rec["N1"]["has_known_distractor"] and rec["N1"]["top1_score"] is None
    rk["N4"] = [dis]
    assert ev.no_answer_records(rk, [n4], CHUNKS, "vector")[0]["top1_is_known_distractor"] is True
    empty = ev.no_answer_records({}, [n4], CHUNKS, "bm25")[0]  # BM25 无词项重叠 -> 排名为空
    assert empty["top1_chunk_id"] is None and empty["top1_is_known_distractor"] is False and empty["n_ranked"] == 0
    # 记录里不含任何"正确/拒答"判断字段
    assert not {"correct", "abstained", "hit1", "rr"} & set(rec["N4"])
    print("✅ test_no_answer_records_probe_types_and_distractor_flag 通过")


def test_analysis_annotations_refer_to_existing_frozen_queries_and_chunks():
    qids = {q["id"] for q in QUERIES if q["type"] == "no_answer"}
    assert ev.INDEX_COVERAGE_PROBES <= qids and set(ev.KNOWN_DISTRACTORS) <= qids
    have = {(c["doc_id"], c["section"]) for c in CHUNKS}
    assert all((d, s) in have for v in ev.KNOWN_DISTRACTORS.values() for d, s in v)
    print("✅ test_analysis_annotations_refer_to_existing_frozen_queries_and_chunks 通过")


def test_target_coverage_raises_when_a_target_cannot_map_to_the_index():
    bad = {"id": "bad", "type": "semantic", "query": "x", "reason": "r",
           "targets": [["doc02_synthmotion_product", "技术布局"], ["doc99_missing", "不存在"]]}
    try:
        ev.target_coverage({}, [bad], CHUNKS)
    except ValueError as e:
        assert "bad" in str(e)
    else:
        raise AssertionError("目标无法映射到索引时必须报错，而不是静默缩小分母")
    single_bad = dict(bad, id="sb", targets=[["doc99_missing", "不存在"]])
    try:
        ev.target_coverage({}, [single_bad], CHUNKS)
    except ValueError:
        pass
    else:
        raise AssertionError("单目标缺失同样必须报错")
    print("✅ test_target_coverage_raises_when_a_target_cannot_map_to_the_index 通过")


def test_bm25_scores_map_to_the_right_chunks_and_match_ranking():
    qs = [{"id": "f1", "type": "keyword", "query": "伺服电机", "targets": [], "reason": "r"},
          {"id": "f2", "type": "keyword", "query": "zzzzqqq不存在的词", "targets": [], "reason": "r"}]
    sc = ev.bm25_scores(qs, CHUNKS)
    rk = ev.bm25_rankings(qs, CHUNKS)
    ids = {c["id"] for c in CHUNKS}
    assert set(sc["f1"]) == set(rk["f1"]) and set(sc["f1"]) <= ids and all(v > 0 for v in sc["f1"].values())
    vals = [sc["f1"][c] for c in rk["f1"]]
    assert vals == sorted(vals, reverse=True), "分数必须与排名同序"
    assert rk["f1"] and sc["f2"] == {} and rk["f2"] == []
    print("✅ test_bm25_scores_map_to_the_right_chunks_and_match_ranking 通过")


def test_no_answer_records_score_belongs_to_the_top1_chunk_and_no_fake_scores():
    n4 = next(q for q in QUERIES if q["id"] == "N4")
    a, b = CHUNKS[0]["id"], CHUNKS[1]["id"]
    rec = ev.no_answer_records({"N4": [b, a]}, [n4], CHUNKS, "bm25", scores={"N4": {a: 9.0, b: 2.5}})[0]
    assert rec["top1_chunk_id"] == b and rec["top1_score"] == 2.5 and rec["score_source"] == "bm25"
    # 没有 scores（vector/hybrid）：不得出现任何分数，也不得声称来源
    for mode in ("vector", "hybrid"):
        r = ev.no_answer_records({"N4": [b, a]}, [n4], CHUNKS, mode)[0]
        assert r["top1_score"] is None and r["score_source"] is None
    print("✅ test_no_answer_records_score_belongs_to_the_top1_chunk_and_no_fake_scores 通过")


def test_build_report_structure_and_provenance_with_fake_data():
    qs = _qs_for_coverage()
    a, b = sorted(ev.target_chunk_ids(qs[0], CHUNKS))
    rk = {"m2": [a, b], "m3": [a], "s1": ["zzz"], "n": [a]}
    rep = ev.build_report(rk, qs, CHUNKS, "bm25", scores={"n": {a: 1.0}})
    base = ev.evaluate(rk, qs, CHUNKS)
    assert {k: rep[k] for k in base} == base, "evaluate() 的原有键必须原样保留"
    assert set(rep) == {"overall", "by_type", "detail", "mode", "target_coverage", "no_answer_records", "provenance"}
    assert rep["mode"] == "bm25" and rep["provenance"]["mode"] == "bm25"
    assert rep["provenance"]["queries_sha256_prefix"] == ev.queries_sha256()[:16]
    assert rep["provenance"]["queries_sha256_prefix"] == "f24a5e0346995cb4", "冻结 v1 查询集被改动"
    assert rep["target_coverage"]["n_queries"] == 2 and rep["no_answer_records"][0]["top1_score"] == 1.0
    json.dumps(rep, ensure_ascii=False)  # 可序列化
    print("✅ test_build_report_structure_and_provenance_with_fake_data 通过")


def test_output_file_is_never_overwritten_without_force():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r.json")
        ev.write_result(p, "one")
        assert open(p, encoding="utf-8").read() == "one"
        for fn in (lambda: ev.write_result(p, "two"), lambda: ev.check_out_path(p, False)):
            try:
                fn()
            except SystemExit as e:
                assert "--force" in str(e)
            else:
                raise AssertionError("已存在的输出文件必须拒绝覆盖")
        assert open(p, encoding="utf-8").read() == "one"
        ev.write_result(p, "three", force=True)
        assert open(p, encoding="utf-8").read() == "three"
        ev.check_out_path(os.path.join(d, "new.json"), False)  # 不存在则放行
        ev.check_out_path(None, False)
    print("✅ test_output_file_is_never_overwritten_without_force 通过")


def test_force_flag_is_output_only_and_checked_before_any_retrieval_work():
    import inspect
    src = inspect.getsource(ev.main)
    assert src.index("check_out_path(") < src.index("load_chunks_and_queries(") < src.index("bm25_rankings(")
    # 审批判断只依赖 --allow-api 与环境变量，不引用 --force
    gate = src[src.index("if not (args.allow_api"):src.index("import chromadb")]
    assert "force" not in gate
    print("✅ test_force_flag_is_output_only_and_checked_before_any_retrieval_work 通过")


def test_fake_openai_key_is_detected_without_echo():
    old = os.environ.get("OPENAI_API_KEY")
    try:
        os.environ["OPENAI_API_KEY"] = "sk-fake-test-key-no-real-network-calls"
        msg = ev.fake_key_problem()
        assert msg and "unset OPENAI_API_KEY" in msg and "sk-fake-test" not in msg
        os.environ["OPENAI_API_KEY"] = "sk-proj-real-looking"
        assert ev.fake_key_problem() is None
        os.environ.pop("OPENAI_API_KEY")
        assert ev.fake_key_problem() is None
    finally:
        if old is not None:
            os.environ["OPENAI_API_KEY"] = old
    import inspect
    src = inspect.getsource(ev.main)
    assert src.index("fake_key_problem()") < src.index("OpenAIEmbeddingFunction(")
    print("✅ test_fake_openai_key_is_detected_without_echo 通过")


def main():
    test_real_kb_has_expected_shape()
    test_every_target_exists_in_the_index()
    test_query_set_structure_and_reasons()
    test_first_hit_rank_and_metrics_hand_computed()
    test_multi_target_query_hits_on_any_target()
    test_vector_candidate_pool_is_capped_and_never_exceeds_collection_size()
    test_hybrid_only_reorders_the_vector_candidate_pool()
    test_api_modes_refuse_without_explicit_approval()
    test_target_coverage_counts_distinct_targets_and_excludes_single_and_no_answer()
    test_real_multi_target_queries_are_exactly_the_known_five()
    test_evaluate_output_unchanged_by_new_analysis()
    test_no_answer_records_probe_types_and_distractor_flag()
    test_analysis_annotations_refer_to_existing_frozen_queries_and_chunks()
    test_target_coverage_raises_when_a_target_cannot_map_to_the_index()
    test_bm25_scores_map_to_the_right_chunks_and_match_ranking()
    test_no_answer_records_score_belongs_to_the_top1_chunk_and_no_fake_scores()
    test_build_report_structure_and_provenance_with_fake_data()
    test_output_file_is_never_overwritten_without_force()
    test_force_flag_is_output_only_and_checked_before_any_retrieval_work()
    test_fake_openai_key_is_detected_without_echo()
    print("\n全部测试通过！")


if __name__ == "__main__":
    main()
