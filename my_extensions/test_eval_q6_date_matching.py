"""eval_q6_sql.py 里 build_date_pattern / check_answer_matches_reference 的
日期归一化单元测试。

背景：Q6 真实 E2E 运行发现，final_report 会把 ground_truth 里的 ISO 日期
（"2025-11-02"）转写成中文日期（"2025年11月2日" / "2025 年 11 月 2 日"），
原来的精确子串匹配会把这种情况误判成"日期不匹配"，但回答本身没有错。
详见 my_extensions/eval_results/q6.json 那次真实运行结果和相关分析。

覆盖范围（正例 4 种 / 负例 3 种 / 边界场景 2 种，均通过唯一入口
check_answer_matches_reference 做集成式测试，因为这正是 eval_q6_sql.py
实际使用这个匹配逻辑的路径）：
  正例：ISO 格式、中文无前导零、中文有前导零、中文带空格（真实观察到的变体）
  负例：同年同月不同日、同月同日不同年、ISO 子串被更长数字串包含
  边界：final_report 为空字符串不报错、换行打断的日期视为已知不支持
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from eval_q6_sql import check_answer_matches_reference

# 与 Q6 真实 ground_truth 一致，只有 round_date 会在各测试用例里被拿来对比，
# amount_usd / round_type 保持不变，避免它们的匹配结果干扰对 date_hit 的判断。
GROUND_TRUTH = {
    "round_type": "Series A",
    "amount_usd": 12000000.0,
    "round_date": "2025-11-02",
}


def _date_hit(final_report: str) -> bool:
    return check_answer_matches_reference(final_report, GROUND_TRUTH)["round_date_matched"]


# ---------------------------------------------------------------------------
# 正例（4 种）
# ---------------------------------------------------------------------------

def test_iso_format_matches():
    assert _date_hit("交易日期：2025-11-02") is True
    print("✅ test_iso_format_matches 通过")


def test_chinese_no_leading_zero_matches():
    assert _date_hit("交易日期：2025年11月2日") is True
    print("✅ test_chinese_no_leading_zero_matches 通过")


def test_chinese_leading_zero_matches():
    assert _date_hit("交易日期：2025年11月02日") is True
    print("✅ test_chinese_leading_zero_matches 通过")


def test_chinese_with_spaces_matches():
    # 本次 Q6 真实运行里 final_report 实际出现过的写法。
    assert _date_hit("此次融资的交易日期为 2025 年 11 月 2 日。") is True
    print("✅ test_chinese_with_spaces_matches 通过（真实观察到的变体）")


# ---------------------------------------------------------------------------
# 负例（3 种，防止边界检查失效导致误判）
# ---------------------------------------------------------------------------

def test_different_day_same_month_does_not_match():
    # "11月12日" 不应该因为尾部含 "2日" 就被误判命中 day=2。
    assert _date_hit("交易日期：2025年11月12日") is False
    assert _date_hit("交易日期：2025年11月20日") is False
    print("✅ test_different_day_same_month_does_not_match 通过")


def test_different_year_does_not_match():
    assert _date_hit("交易日期：2024年11月2日") is False
    print("✅ test_different_year_does_not_match 通过")


def test_longer_digit_string_does_not_match():
    # "2025-11-02" 是 "2025-11-020" 的前缀子串，没有 (?!\d) 边界保护的话
    # 会被误判命中——这是把金额匹配那次修复的同款问题复现在日期上。
    assert _date_hit("报告编号：2025-11-020，请核对") is False
    print("✅ test_longer_digit_string_does_not_match 通过")


# ---------------------------------------------------------------------------
# 边界场景（2 种）
# ---------------------------------------------------------------------------

def test_empty_final_report_does_not_raise():
    assert _date_hit("") is False
    print("✅ test_empty_final_report_does_not_raise 通过")


def test_newline_broken_date_does_not_match():
    # 已知限制，故意不支持：日期表达式被换行符打断时不匹配。
    # 这不是遗漏，是设计上不用 \s（会连换行符一起放行），这条测试把这个
    # 限制显式固定下来，避免以后被误改成"顺手就支持了"又没人注意到。
    assert _date_hit("交易日期为2025年\n11月2日，请查收") is False
    print("✅ test_newline_broken_date_does_not_match 通过（已知限制，非遗漏）")


def main():
    test_iso_format_matches()
    test_chinese_no_leading_zero_matches()
    test_chinese_leading_zero_matches()
    test_chinese_with_spaces_matches()
    test_different_day_same_month_does_not_match()
    test_different_year_does_not_match()
    test_longer_digit_string_does_not_match()
    test_empty_final_report_does_not_raise()
    test_newline_broken_date_does_not_match()
    print("\n全部日期归一化单元测试通过！")


if __name__ == "__main__":
    main()
