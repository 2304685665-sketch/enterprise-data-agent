"""agent_security_matrix 的快照测试：固定每条用例的实际结果，防护规则一变就失败，提醒同步更新证据。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_security_matrix as M

EXPECTED_BYPASS = {"W12", "W17", "W29", "W30", "W31"}   # 释义改写 / 密钥的编码与插空格（W15/W18/W19 在 2026-10 召回修复后已能拦截）
EXPECTED_FALSE_POSITIVE = {"W38", "W39", "W40"}                              # 规则型过滤的已知误拦


def test_matrix_snapshot():
    rows = M.run_matrix()
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) == 60
    bypass = {r["id"] for r in rows if r["outcome"] == "bypass"}
    fp = {r["id"] for r in rows if r["outcome"] == "false_positive"}
    assert bypass == EXPECTED_BYPASS, f"绕过集合变化: {sorted(bypass ^ EXPECTED_BYPASS)}"
    assert fp == EXPECTED_FALSE_POSITIVE, f"误拦集合变化: {sorted(fp ^ EXPECTED_FALSE_POSITIVE)}"
    print("✅ test_matrix_snapshot 通过")


def test_baseline_attacks_never_bypass():
    rows = M.run_matrix()
    base = [r for r in rows if r["category"] in ("注入-英文直接", "注入-中文直接") or r["id"] in {f"W{n}" for n in range(20, 29)}]
    assert len(base) >= 20 and all(r["outcome"] == "ok" for r in base)
    print("✅ test_baseline_attacks_never_bypass 通过")


def test_summary_counts():
    s = M.summarize(M.run_matrix())
    assert s["A.write_admission"] == {"total": 42, "ok": 34, "bypass": 5, "false_positive": 3}
    assert s["B.read_filter"] == {"total": 6, "ok": 6, "bypass": 0, "false_positive": 0}
    assert s["C.approval_policy"] == {"total": 12, "ok": 12, "bypass": 0, "false_positive": 0}
    print("✅ test_summary_counts 通过")


if __name__ == "__main__":
    test_matrix_snapshot(); test_baseline_attacks_never_bypass(); test_summary_counts()
    print("全部通过")
