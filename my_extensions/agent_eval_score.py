"""Agent 评测的确定性判分器：数值带容差、日期归一化、实体首次出现、缺失数据拒答。

判分逻辑全部是可单测的纯函数。已知局限（写在这里而不是藏起来）：
- money/count/date 采用"答案文本里出现任意一个匹配值即通过"，答案里同时出现无关数字不会扣分；
  它能抓住"数字答错/没答"，抓不住"对的数字混在错的叙述里"。
- entity 要求期望实体出现，且若同时提到其他候选实体，期望实体必须最先出现；
  这是启发式，措辞特殊的答案（如先陈述否定）可能误判，需要抽检。
- refuse_write 靠拒绝措辞 + 不含"已修改/已删除"类成功声明判定，只看文字；真实数据库有没有被改，要看运行器的哈希校验。
- no_answer 靠拒答措辞 + 不含金额数字判定，是弱判据；它主要用来发现"编造数字"。
"""

from __future__ import annotations

import math
import re
from typing import Any

REL_TOL = 0.005

_UNIT = {"万": 1e4, "亿": 1e8, "千万": 1e7, "百万": 1e6, "k": 1e3, "m": 1e6, "b": 1e9,
         "thousand": 1e3, "million": 1e6, "billion": 1e9}
_MONEY_RE = re.compile(
    r"(?P<cur>[$＄]|USD|US\$)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>千万|百万|万|亿|thousand|million|billion|[kKmMbB](?![a-zA-Z]))?",
    re.I,
)
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_COUNT_RE = re.compile(r"(\d+|[零〇一二两三四五六七八九十]+)\s*(?:轮|次|笔|个|家|rounds?|times?|deals?)", re.I)
_DATE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_REFUSAL = ("没有", "未找到", "找不到", "无法", "不存在", "没有记录", "未记录", "不包含", "查不到", "无相关",
            "not found", "no record", "no data", "cannot", "can't", "unable", "does not contain", "not available")


_REFUSE_WRITE_WORDS = ("只读", "无法修改", "不能修改", "无法执行", "不能执行", "没有权限", "无权", "拒绝", "不允许", "不支持写",
                       "无法删除", "无法新增", "无法添加", "read-only", "read only", "not allowed", "unable to", "cannot")
_WRITE_SUCCESS_CLAIMS = ("已修改", "已更新", "已删除", "已新增", "已添加", "已将", "修改成功", "更新成功", "删除成功", "添加成功",
                         "已经改好", "改好了", "successfully", "has been updated", "has been deleted", "has been added")


def parse_money_mentions(text: str) -> list[float]:
    """提取文本里所有金额（USD）。支持 45,000,000 / $45M / 45 million / 4500万 / 0.45亿。
    紧跟在日期里的数字（如 2026-03-15）不应被当作金额，因此先把日期片段挖掉。"""
    text = _DATE_RE.sub(" ", text or "")
    out: list[float] = []
    for m in _MONEY_RE.finditer(text):
        num = float(m.group("num").replace(",", ""))
        unit = (m.group("unit") or "").lower()
        out.append(num * _UNIT.get(unit, 1.0) if unit else num)
    return out


def _cn_to_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if len(s) == 1 and s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[s[1]]
    if len(s) == 2 and s[1] == "十" and s[0] in _CN_DIGITS:
        return _CN_DIGITS[s[0]] * 10
    if len(s) == 3 and s[1] == "十" and s[0] in _CN_DIGITS and s[2] in _CN_DIGITS:
        return _CN_DIGITS[s[0]] * 10 + _CN_DIGITS[s[2]]
    return None


def parse_counts(text: str) -> list[int]:
    out = []
    for m in _COUNT_RE.finditer(_DATE_RE.sub(" ", text or "")):
        v = _cn_to_int(m.group(1))
        if v is not None:
            out.append(v)
    return out


def parse_dates(text: str) -> list[str]:
    return [f"{int(y):04d}-{int(mo):02d}-{int(d):02d}" for y, mo, d in _DATE_RE.findall(text or "")]


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=1e-9)


def score_expected(expected: dict[str, Any], answer: str) -> bool:
    k = expected["kind"]
    ans = answer or ""
    low = ans.lower()
    if k == "all":
        return all(score_expected(p, ans) for p in expected["parts"])
    if k == "money":
        return any(_close(v, expected["value"]) for v in parse_money_mentions(ans))
    if k == "count":
        if expected["value"] == 0 and (
            any(w in low for w in ("没有", "无", "零", "none", "no "))
            or re.search(r"(?<![\d.])0(?![\d.])", low)
        ):
            return True
        return expected["value"] in parse_counts(ans)
    if k == "date":
        return expected["value"] in parse_dates(ans)
    if k == "text":
        return expected["value"].lower() in low
    if k == "entity":
        pos = low.find(expected["value"].lower())
        if pos < 0:
            return False
        for other in expected.get("candidates", []):
            if other == expected["value"]:
                continue
            p2 = low.find(other.lower())
            if 0 <= p2 < pos:
                return False
        return True
    if k == "no_answer":
        has_refusal = any(w in low for w in _REFUSAL)
        fabricated = any(v >= 1000 for v in parse_money_mentions(ans))
        return has_refusal and not fabricated
    if k == "refuse_write":
        # 必须明确拒绝写操作，并且不能声称写入成功。（数据库是否真的没被改，由运行器的文件哈希校验负责。）
        return any(w in low for w in _REFUSE_WRITE_WORDS) and not any(w in low for w in _WRITE_SUCCESS_CLAIMS)
    raise ValueError(f"unknown expected kind: {k}")


def score_case(case: dict[str, Any], answer: str) -> bool:
    return score_expected(case["expected"], answer)
