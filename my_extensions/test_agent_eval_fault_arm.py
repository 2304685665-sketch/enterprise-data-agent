"""故障臂离线测试：注入比例、可复现、与真实 execute_tool_safely 的重试配合。"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")
from open_deep_research import deep_researcher as dr
import agent_eval_fault_arm as fa

class T:
    name = "internal_doc_search"
    async def ainvoke(self, *a, **k): raise AssertionError

def run(p, retries, n, case):
    async def ok(*a, **k): return "ok"
    dr._RETRY_AWARE_TOOL_CALLS["internal_doc_search"] = ok
    pre, post = fa.install(dr, p, 7)
    cfg = types.SimpleNamespace(tool_retry_max_attempts=retries, tool_retry_base_delay_seconds=0.0)
    pre({"id": case}); res = []
    for _ in range(n):
        res.append(asyncio.run(dr.execute_tool_safely(T(), {"query": "q"}, config=None, configurable=cfg))[1])
    r = {}; post({"id": case}, r); return res.count("success"), r["fault"]

def test_p0_never_injects():
    ok, f = run(0.0, 2, 50, "a"); assert ok == 50 and f["injected"] == 0 and f["doc_search_attempts"] == 50
    print("✅ test_p0_never_injects")

def test_same_case_same_sequence_and_retry_helps():
    a, fa_ = run(0.5, 0, 200, "x"); b, fb = run(0.5, 0, 200, "x")
    assert (a, fa_) == (b, fb)
    c, _ = run(0.5, 2, 200, "x")
    assert c > a and 0.35 < a / 200 < 0.65
    print("✅ test_same_case_same_sequence_and_retry_helps", a, c)

def test_different_case_different_sequence():
    assert run(0.5, 0, 200, "x")[1] != run(0.5, 0, 200, "y")[1]
    print("✅ test_different_case_different_sequence")

if __name__ == "__main__":
    test_p0_never_injects(); test_same_case_same_sequence_and_retry_helps(); test_different_case_different_sequence(); print("全部测试通过")
