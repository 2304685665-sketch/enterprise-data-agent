import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_eval_score_v3 as v3

CASE = {"expected": {"kind": "count", "value": 2014}}


def test_year_answer_accepted_in_common_phrasings():
    assert v3.score_case_v3(CASE, "该公司成立于 **2014年**。", True, [])
    assert v3.score_case_v3(CASE, "成立年份为2014，融资发生在 2014 年之后。", True, [])


def test_wrong_or_missing_year_rejected():
    assert not v3.score_case_v3(CASE, "该公司成立于 2016年。", True, [])
    assert not v3.score_case_v3(CASE, "未能查到成立年份。", True, [])


def test_minority_year_rejected_when_other_year_dominates():
    assert not v3.score_case_v3(CASE, "2016年成立；2016年融资；2016年上市；也有人说 2014年。", True, [])


def test_non_year_counts_unchanged():
    c = {"expected": {"kind": "count", "value": 12}}
    assert v3.score_case_v3(c, "一共有 12 家公司。", True, [])
    assert not v3.score_case_v3(c, "一共有 13 家公司。", True, [])


def test_measure_word_wei_and_others_accepted():
    c = {"expected": {"kind": "count", "value": 25}}
    for ans in ("客户总数为 **25位**。", "共有25名客户", "一共 25 条记录", "共 25 人"):
        assert v3.score_case_v3(c, ans, True, []), ans
    assert not v3.score_case_v3(c, "客户总数为 24位。", True, [])
    assert not v3.score_case_v3(c, "没有给出数字", True, [])


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("✅", n)
