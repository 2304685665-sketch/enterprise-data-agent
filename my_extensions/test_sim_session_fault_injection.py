"""会话级故障注入的离线测试（驱动真实 researcher_tools，虚拟时钟，不调用任何 API）。"""
import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-sim")

import sim_session_fault_injection as sim  # noqa: E402


def run(cfg, fm, seed):
    return asyncio.run(sim.run_session(cfg, fm, seed))


def avg(cfg, fm, key, seeds=range(8)):
    return statistics.mean(run(cfg, fm, 100 + s)[key] for s in seeds)


def test_deterministic_for_same_seed():
    assert run("retry+breaker", "burst", 7) == run("retry+breaker", "burst", 7)
    print("✅ test_deterministic_for_same_seed 通过")


def test_no_retry_one_attempt_per_call_and_never_blocked():
    r = run("no_retry", "iid", 1)
    assert r["attempts"] == r["calls"] and r["blocked_by_breaker"] == 0
    assert 0.25 <= r["attempt_failure_rate"] <= 0.35, r["attempt_failure_rate"]
    print("✅ test_no_retry_one_attempt_per_call_and_never_blocked 通过")


def test_iid_retry_matches_analytic_success_rate():
    base, with_retry = avg("no_retry", "iid", "call_success_rate"), avg("retry", "iid", "call_success_rate")
    assert abs(base - 0.70) < 0.05 and abs(with_retry - (1 - 0.3 ** 3)) < 0.04, (base, with_retry)
    print("✅ test_iid_retry_matches_analytic_success_rate 通过")


def test_burst_outage_fraction_is_about_30_percent_and_retry_helps_less_than_iid():
    frac = avg("no_retry", "burst", "outage_fraction", seeds=range(20))
    assert 0.2 <= frac <= 0.4, frac
    gain_iid = avg("retry", "iid", "call_success_rate") - avg("no_retry", "iid", "call_success_rate")
    gain_burst = avg("retry", "burst", "call_success_rate") - avg("no_retry", "burst", "call_success_rate")
    assert 0 < gain_burst < gain_iid, (gain_burst, gain_iid)
    print("✅ test_burst_outage_fraction_is_about_30_percent_and_retry_helps_less_than_iid 通过")


def test_breaker_cuts_attempts_but_does_not_raise_success_under_burst():
    ra = avg("retry", "burst", "attempts", seeds=range(15))
    rb = avg("retry+breaker", "burst", "attempts", seeds=range(15))
    sa = avg("retry", "burst", "call_success_rate", seeds=range(15))
    sb = avg("retry+breaker", "burst", "call_success_rate", seeds=range(15))
    blocked = avg("retry+breaker", "burst", "blocked_by_breaker", seeds=range(15))
    assert blocked > 0 and rb < ra, (blocked, ra, rb)
    assert sb <= sa + 1e-9, "熔断不应被报告为提高成功率"
    print("✅ test_breaker_cuts_attempts_but_does_not_raise_success_under_burst 通过")


if __name__ == "__main__":
    for f in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        f()
    print("全部通过")
