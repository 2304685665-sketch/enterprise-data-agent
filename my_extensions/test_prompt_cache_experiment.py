"""离线测试：不调用 API。验证前缀构造、CONTROL 臂前缀不同、成本折算、汇总、预算守卫。"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prompt_cache_experiment as P


def test_arms_and_rounds_structure():
    p = "PREFIX"
    a = P.make_messages(p, "q", 3); b = P.make_messages(p, "q", 3)
    assert a == b and len(a) == 2 + 2 * 2
    c = P.make_messages(p, "q", 3, nonce="n1"); d = P.make_messages(p, "q", 3, nonce="n2")
    assert c[0] != d[0] and c[1:] == d[1:]
    # 第 k 轮是第 k-1 轮的严格扩展（前缀缓存成立的前提）
    assert P.make_messages(p, "q", 4)[:len(a)] == a


def test_cost_and_summary():
    assert abs(P.cost(1000, 0) - 0.0004) < 1e-12 and abs(P.cost(1000, 1000) - 0.0001) < 1e-12
    rows = [{"arm": "CACHED", "round": 2, "prompt_tokens": 2000, "cached_tokens": 1500, "cost_usd": P.cost(2000, 1500), "cost_without_cache_usd": P.cost(2000, 0)},
            {"arm": "CONTROL", "round": 2, "prompt_tokens": 2000, "cached_tokens": 0, "cost_usd": P.cost(2000, 0), "cost_without_cache_usd": P.cost(2000, 0)}]
    s = P.summarize(rows)
    assert s["CACHED/round>=2"]["hit_rate"] == 0.75 and s["CONTROL/round>=2"]["hit_rate"] == 0.0


def test_budget_guard_stops_before_overspend():
    class C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    u = types.SimpleNamespace(prompt_tokens=10_000_000, prompt_tokens_details=types.SimpleNamespace(cached_tokens=0))
                    return types.SimpleNamespace(usage=u)
    P.time.sleep = lambda s: None
    try:
        P.run(C, None)
    except SystemExit as e:
        assert "超出预算" in str(e)
    else:
        raise AssertionError("预算守卫未触发")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("✅", n)
