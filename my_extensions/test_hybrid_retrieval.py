"""hybrid_retrieval（BM25 + RRF 混合检索排序）的离线单元测试。

全部是纯函数测试，不需要 API、不需要 Chroma、不需要 Docker。
断言里的 RRF 分数都是手算值（见各测试注释），不是"跑出来什么就写什么"。
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from open_deep_research.hybrid_retrieval import (  # noqa: E402
    BM25Index,
    hybrid_rank,
    reciprocal_rank_fusion,
    tokenize,
)


# ---------------- tokenize ----------------

def test_tokenize_mixed_english_digits_and_cjk_bigrams():
    toks = tokenize("Series C 融资 8000万美元")
    # 英文数字词：series / c / 8000；汉字：融资 -> [融资]；万美元 -> [万美, 美元]
    assert sorted(toks) == sorted(["series", "c", "8000", "融资", "万美", "美元"]), toks
    print("✅ test_tokenize_mixed_english_digits_and_cjk_bigrams 通过")


def test_tokenize_single_cjk_char_kept_and_separators_dropped():
    assert tokenize("A 的 B") == ["a", "b", "的"], tokenize("A 的 B")
    assert sorted(tokenize("doc01_synthmotion")) == ["doc01", "synthmotion"]
    assert tokenize("") == [] and tokenize("  ,。！ ") == []
    print("✅ test_tokenize_single_cjk_char_kept_and_separators_dropped 通过")


# ---------------- BM25 ----------------

DOCS = [
    "SynthMotion 成立于2022年 总部位于上海",
    "RoboWorks 技术路线 总部位于北京",
    "AtlasAI 技术布局 总部位于深圳",
]


def test_bm25_rare_term_ranks_its_document_first():
    idx = BM25Index(DOCS)
    assert idx.rank("SynthMotion") == [0]
    assert idx.rank("RoboWorks") == [1]
    print("✅ test_bm25_rare_term_ranks_its_document_first 通过")


def test_bm25_idf_rare_term_higher_than_common_term_and_non_negative():
    idx = BM25Index(DOCS)
    assert idx._idf("synthmotion") > idx._idf("总部"), "稀有词 idf 必须高于出现在全部文档里的词"
    assert idx._idf("总部") >= 0, "非负 idf 变体：即使词项出现在全部文档里也不能为负"
    # 手算：N=3, n=1 -> ln(1 + (3-1+0.5)/(1+0.5)) = ln(1+2.5/1.5)
    assert math.isclose(idx._idf("synthmotion"), math.log(1 + 2.5 / 1.5))
    print("✅ test_bm25_idf_rare_term_higher_than_common_term_and_non_negative 通过")


def test_bm25_length_normalization_prefers_shorter_doc_with_same_tf():
    docs = ["alpha beta", "alpha " + " ".join(f"w{i}" for i in range(30))]
    sc = BM25Index(docs).scores("alpha")
    assert sc[0] > sc[1] > 0, sc
    print("✅ test_bm25_length_normalization_prefers_shorter_doc_with_same_tf 通过")


def test_bm25_no_overlap_gives_empty_rank_and_zero_scores():
    idx = BM25Index(DOCS)
    assert idx.scores("zzzz qqqq") == [0.0, 0.0, 0.0]
    assert idx.rank("zzzz qqqq") == []
    print("✅ test_bm25_no_overlap_gives_empty_rank_and_zero_scores 通过")


def test_bm25_top_n_and_tie_break_by_index_and_empty_index():
    idx = BM25Index(["same text", "same text", "same text"])
    assert idx.rank("same", top_n=2) == [0, 1], "完全同分按下标升序，保证确定性"
    assert BM25Index([]).scores("x") == [] and BM25Index([]).rank("x") == []
    print("✅ test_bm25_top_n_and_tie_break_by_index_and_empty_index 通过")


# ---------------- RRF ----------------

def test_rrf_hand_computed_scores_and_deterministic_tiebreak():
    # k=60：a = 1/61 + 1/62，b = 1/62 + 1/61（相等），c = 1/63，d = 1/63（相等）
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]], k=60)
    assert [i for i, _ in fused] == ["a", "b", "c", "d"], fused
    sc = dict(fused)
    assert math.isclose(sc["a"], 1 / 61 + 1 / 62) and math.isclose(sc["b"], 1 / 61 + 1 / 62)
    assert math.isclose(sc["c"], 1 / 63) and math.isclose(sc["d"], 1 / 63)
    assert fused == reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]], k=60), "必须确定性"
    print("✅ test_rrf_hand_computed_scores_and_deterministic_tiebreak 通过")


def test_rrf_item_in_both_rankings_beats_item_high_in_only_one():
    # y = 1/62 + 1/62 ≈ 0.03226 > x = 1/61 ≈ 0.01639
    fused = reciprocal_rank_fusion([["x", "y"], ["z", "y"]], k=60)
    assert fused[0][0] == "y", fused
    print("✅ test_rrf_item_in_both_rankings_beats_item_high_in_only_one 通过")


def test_rrf_dedupes_within_list_and_handles_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
    fused = dict(reciprocal_rank_fusion([["a", "a", "b"]], k=60))
    assert math.isclose(fused["a"], 1 / 61), "同一列表内重复 id 只计第一次"
    # b 在原列表里是第 3 个元素（enumerate 位置 3），所以是 1/(60+3)，不是 1/62
    assert math.isclose(fused["b"], 1 / 63), fused
    print("✅ test_rrf_dedupes_within_list_and_handles_empty 通过")


# ---------------- hybrid_rank ----------------

IDS = ["c1", "c2", "c3"]
TEXTS = [
    "SynthMotion Series C 融资 8000万美元",
    "RoboWorks 技术路线 双足",
    "AtlasAI 技术布局 视觉",
]


def test_hybrid_recovers_lexical_match_missed_by_vector_ranking():
    # 向量排名完全漏掉 c1；BM25 靠 "series"/"c"/"融资" 把它找回来
    out = hybrid_rank("Series C 融资", IDS, TEXTS, vector_ranked_ids=["c2", "c3"], top_n=2)
    assert "c1" in out, out
    print("✅ test_hybrid_recovers_lexical_match_missed_by_vector_ranking 通过")


def test_hybrid_falls_back_to_vector_order_when_bm25_has_no_overlap():
    out = hybrid_rank("zzzz", IDS, TEXTS, vector_ranked_ids=["c3", "c1", "c2"], top_n=2)
    assert out == ["c3", "c1"], out
    print("✅ test_hybrid_falls_back_to_vector_order_when_bm25_has_no_overlap 通过")


def test_hybrid_rejects_mismatched_lengths_and_is_deterministic():
    try:
        hybrid_rank("q", ["a", "b"], ["only one"], ["a"], top_n=1)
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass
    a = hybrid_rank("Series C 融资", IDS, TEXTS, ["c2", "c3"], 3)
    b = hybrid_rank("Series C 融资", IDS, TEXTS, ["c2", "c3"], 3)
    assert a == b
    print("✅ test_hybrid_rejects_mismatched_lengths_and_is_deterministic 通过")


# ---------------- 真实知识库切分结果上的 BM25 ----------------

def test_bm25_on_real_knowledge_base_chunks_finds_expected_section():
    kb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base")
    # 用与 build_knowledge_base.split_into_chunks 相同的切分规则（按 "## " 标题），
    # 不 import 该脚本——它在 import 时就会构造 OpenAI Embedding 对象。
    chunks = []
    for fn in sorted(os.listdir(kb_dir)):
        if not fn.endswith(".md") or fn == "ANSWER_KEY.md":
            continue
        title, buf = None, []
        for line in open(os.path.join(kb_dir, fn), encoding="utf-8").read().split("\n"):
            if line.startswith("## "):
                if title and "".join(buf).strip():
                    chunks.append((fn[:-3], title, "\n".join(buf).strip()))
                title, buf = line[3:].strip(), [line]
            else:
                buf.append(line)
        if title and "".join(buf).strip():
            chunks.append((fn[:-3], title, "\n".join(buf).strip()))
    assert len(chunks) >= 20, f"真实知识库切分结果应不少于 20 个 chunk，实际 {len(chunks)}"
    idx = BM25Index([c[2] for c in chunks])
    top = idx.rank("SynthMotion 最近一轮融资 Series C", top_n=1)[0]
    assert chunks[top][0] == "doc01_synthmotion_intro" and chunks[top][1] == "融资历程", chunks[top][:2]
    print(f"✅ test_bm25_on_real_knowledge_base_chunks_finds_expected_section 通过（{len(chunks)} 个 chunk）")


def main():
    test_tokenize_mixed_english_digits_and_cjk_bigrams()
    test_tokenize_single_cjk_char_kept_and_separators_dropped()
    test_bm25_rare_term_ranks_its_document_first()
    test_bm25_idf_rare_term_higher_than_common_term_and_non_negative()
    test_bm25_length_normalization_prefers_shorter_doc_with_same_tf()
    test_bm25_no_overlap_gives_empty_rank_and_zero_scores()
    test_bm25_top_n_and_tie_break_by_index_and_empty_index()
    test_rrf_hand_computed_scores_and_deterministic_tiebreak()
    test_rrf_item_in_both_rankings_beats_item_high_in_only_one()
    test_rrf_dedupes_within_list_and_handles_empty()
    test_hybrid_recovers_lexical_match_missed_by_vector_ranking()
    test_hybrid_falls_back_to_vector_order_when_bm25_has_no_overlap()
    test_hybrid_rejects_mismatched_lengths_and_is_deterministic()
    test_bm25_on_real_knowledge_base_chunks_finds_expected_section()
    print("\n全部测试通过！")


if __name__ == "__main__":
    main()
