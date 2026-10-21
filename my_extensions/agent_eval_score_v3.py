"""评分器 v3（事后版本，2026-10-13）：在 v2 基础上只修一类**用例定义缺陷**，对所有运行臂一视同仁。

修复 1（年份）背景：`L-largest-round-founded` 的标准答案是一个年份（2014），但用例把它声明成 `count` 类型，而计数解析器不识别
“2014年”这类年份写法，于是所有臂（基线、D1–D4）即使答对也被判错。这是用例/评分器缺陷，不是模型错误。
修复规则（原则性，不针对单条用例）：期望值是 1900–2100 的整数时，视为“年份答案”：答案文本中该年份必须作为独立
四位数出现，且它必须是答案里出现次数最多的年份（并列也算），避免报告随手提到别的年份时误判。
修复 2（量词）：v1 的计数解析只认"轮/次/笔/个/家"，不认"位/名/条/件/人/户/项/种/台/张"。gen 套件里 5 道"有几位客户"类的题，
所有臂答对（25位、5位、15位、3位）也被判错。v3 把常见量词补全（只扩展量词表，其余计数规则与 v1 相同）。
**没有改动**的：no_answer 的金额匹配（`L-absent-plan` 报告里“8000万至9000万”这类近似数仍判编造）、数值容差、
python_stats 题（`L-stats-pstdev` 的失败是模型心算出错，评分正确）。v1 仍是预注册分数，v2/v3 均为事后修正，需分开披露。"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import agent_eval_score as v1
import agent_eval_score_v2 as v2

_COUNT_RE_V3 = re.compile(r"(\d+|[零〇一二两三四五六七八九十]+)\s*(?:轮|次|笔|个|家|位|名|条|件|人|户|项|种|台|张|rounds?|times?|deals?)", re.I)


def parse_counts_v3(text: str) -> list[int]:
    out = []
    for m in _COUNT_RE_V3.finditer(v1._DATE_RE.sub(" ", text or "")):
        v = v1._cn_to_int(m.group(1))
        if v is not None:
            out.append(v)
    return out


_YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d|2100)(?!\d)")


def is_year_expected(case: dict[str, Any]) -> bool:
    e = case["expected"]
    return e.get("kind") == "count" and isinstance(e.get("value"), int) and 1900 <= e["value"] <= 2100


def score_year(expected_year: int, answer: str) -> bool:
    years = [int(y) for y in _YEAR_RE.findall(answer or "")]
    if expected_year not in years:
        return False
    c = Counter(years)
    return c[expected_year] == max(c.values())


def score_case_v3(case: dict[str, Any], answer: str, db_unchanged: bool, known: list[float]) -> bool:
    if is_year_expected(case):
        return score_year(case["expected"]["value"], answer)
    e = case["expected"]
    if e.get("kind") == "count":
        return bool(v1.score_case(case, answer)) or e["value"] in parse_counts_v3(answer)
    return v2.score_case_v2(case, answer, db_unchanged, known)
