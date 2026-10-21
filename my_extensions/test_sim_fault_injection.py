"""故障注入模拟的离线测试：验证模拟本身可信（种子可复现、与解析值吻合、上限与分类正确）。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import sim_fault_injection as sim  # noqa: E402


def cell(p, r, n=3000, seed=7, permanent=False):
    return asyncio.run(sim.run_cell(p, r, n, seed, permanent))


def test_reproducible_with_same_seed():
    assert cell(0.3, 2) == cell(0.3, 2)
    print("✅ test_reproducible_with_same_seed 通过")


def test_success_rate_matches_analytical_within_tolerance():
    # n=3000 时标准误 <= 0.01；容差取 0.03（约 3 个标准误以上）
    for p in (0.1, 0.3, 0.5):
        for r in (0, 1, 2, 3):
            c = cell(p, r)
            assert abs(c["success_rate"] - c["analytical_success_rate"]) < 0.03, c
    print("✅ test_success_rate_matches_analytical_within_tolerance 通过")


def test_more_retries_never_hurt_and_attempts_bounded():
    prev = -1.0
    for r in (0, 1, 2, 3):
        c = cell(0.5, r)
        assert c["max_attempts"] <= r + 1, c
        assert c["success_rate"] >= prev - 0.02, (r, c)
        prev = c["success_rate"]
    print("✅ test_more_retries_never_hurt_and_attempts_bounded 通过")


def test_exhausted_retries_classified_as_infrastructure_error():
    c = cell(1.0, 2, n=50)  # 必然失败
    assert c["success_rate"] == 0 and c["failure_kinds"] == {"infrastructure_error": 50}, c
    assert c["max_attempts"] == 3 and c["mean_attempts"] == 3
    print("✅ test_exhausted_retries_classified_as_infrastructure_error 通过")


def test_permanent_errors_are_never_retried():
    c = cell(1.0, 3, n=50, permanent=True)
    assert c["max_attempts"] == 1 and c["success_rate"] == 0, c
    print("✅ test_permanent_errors_are_never_retried 通过")


def test_no_failures_means_single_attempt():
    c = cell(0.0, 3, n=100)
    assert c["success_rate"] == 1 and c["mean_attempts"] == 1 and c["failure_kinds"] == {"none": 100}, c
    print("✅ test_no_failures_means_single_attempt 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
