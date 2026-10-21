"""SQL 攻击/正常/资源用例集及三版本审计结果的离线回归（不重新运行 eval，只校验用例集与已保存结果）。
三个版本都来自真实 git 历史：591b456（初版）、69de947（加入 PRAGMA 拦截）、HEAD（2026-10-12）。
用例 A24–A42、N13–N16 是在看过旧用例结果之后新增的，不是预注册。"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sql_guard_cases as c  # noqa: E402

RES = os.path.join(HERE, "sql_guard_audit_results")


def tally(name):
    rows = json.load(open(os.path.join(RES, name), encoding="utf-8"))
    return rows, dict(collections.Counter((r["id"][0], r["verdict"]) for r in rows))


def test_case_counts_and_unique_ids():
    ids = [x["id"] for x in c.ATTACK_CASES + c.NORMAL_CASES + c.RESOURCE_CASES]
    assert len(ids) == len(set(ids)) == 60
    assert (len(c.ATTACK_CASES), len(c.NORMAL_CASES), len(c.RESOURCE_CASES)) == (42, 16, 2)
    print("✅ test_case_counts_and_unique_ids 通过")


def test_saved_results_for_three_real_versions():
    _, init = tally("sql_guard_initial_591b456.json")
    assert init[("A", "攻击成功")] == 3 and init[("N", "正常查询被误拦")] == 1 and init[("A", "攻击被拦截")] == 37
    rows, mid = tally("sql_guard_mid_69de947.json")
    assert ("A", "攻击成功") not in mid and mid[("N", "正常查询被误拦")] == 1
    rows, head = tally("sql_guard_head_20261012.json")
    assert ("A", "攻击成功") not in head and ("N", "正常查询被误拦") not in head
    assert head[("A", "攻击被拦截")] == 40 and head[("A", "测试未能验证")] == 2 and head[("N", "正常查询通过")] == 16
    assert head[("R", "攻击被拦截")] == 2
    succeeded = sorted(r["id"] for r in json.load(open(os.path.join(RES, "sql_guard_initial_591b456.json"), encoding="utf-8")) if r["verdict"] == "攻击成功")
    assert succeeded == ["A16", "A23", "A42"], succeeded  # 初版唯一的漏洞类别：PRAGMA 写文件头/切换日志模式
    print("✅ test_saved_results_for_three_real_versions 通过")


if __name__ == "__main__":
    test_case_counts_and_unique_ids(); test_saved_results_for_three_real_versions(); print("全部通过")
