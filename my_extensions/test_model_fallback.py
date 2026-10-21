"""模型降级的离线测试（假模型，无任何 API 调用），含接入 researcher 节点的回归。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")

import httpx  # noqa: E402
import openai  # noqa: E402

from open_deep_research.model_fallback import AllModelsFailed, ainvoke_with_fallback, is_fallback_worthy  # noqa: E402

_REQ = httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def _resp(code):
    return httpx.Response(code, request=_REQ)


class Fake:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def run(c):
    return asyncio.run(c)


def test_classification_of_exceptions():
    assert is_fallback_worthy(openai.APIConnectionError(request=_REQ))
    assert is_fallback_worthy(openai.APITimeoutError(request=_REQ))
    assert is_fallback_worthy(openai.RateLimitError("rl", response=_resp(429), body=None))
    assert is_fallback_worthy(openai.InternalServerError("5xx", response=_resp(500), body=None))
    assert is_fallback_worthy(asyncio.TimeoutError())
    assert not is_fallback_worthy(openai.AuthenticationError("bad key", response=_resp(401), body=None))
    assert not is_fallback_worthy(openai.BadRequestError("too long", response=_resp(400), body=None))
    assert not is_fallback_worthy(ValueError("x"))
    print("✅ test_classification_of_exceptions 通过")


def test_primary_success_never_touches_fallback():
    a, b = Fake("A"), Fake("B")
    assert run(ainvoke_with_fallback([("primary", a), ("fallback", b)], [])) == ("A", "primary")
    assert (a.calls, b.calls) == (1, 0)
    print("✅ test_primary_success_never_touches_fallback 通过")


def test_worthy_failure_falls_back_in_order():
    a = Fake(exc=openai.APITimeoutError(request=_REQ))
    b = Fake("B")
    assert run(ainvoke_with_fallback([("primary", a), ("fallback", b)], [])) == ("B", "fallback")
    assert (a.calls, b.calls) == (1, 1)
    print("✅ test_worthy_failure_falls_back_in_order 通过")


def test_request_errors_do_not_fall_back_and_propagate_unchanged():
    err = openai.AuthenticationError("bad key", response=_resp(401), body=None)
    a, b = Fake(exc=err), Fake("B")
    try:
        run(ainvoke_with_fallback([("primary", a), ("fallback", b)], []))
        raise AssertionError("应抛出")
    except openai.AuthenticationError as e:
        assert e is err
    assert b.calls == 0
    print("✅ test_request_errors_do_not_fall_back_and_propagate_unchanged 通过")


def test_all_fail_raises_with_per_model_errors():
    a = Fake(exc=openai.APIConnectionError(request=_REQ))
    b = Fake(exc=openai.RateLimitError("rl", response=_resp(429), body=None))
    try:
        run(ainvoke_with_fallback([("primary", a), ("fallback", b)], []))
        raise AssertionError("应抛出")
    except AllModelsFailed as e:
        assert [l for l, _ in e.errors] == ["primary", "fallback"]
    print("✅ test_all_fail_raises_with_per_model_errors 通过")


def test_empty_candidates_rejected():
    try:
        run(ainvoke_with_fallback([], []))
        raise AssertionError("应抛出")
    except ValueError:
        pass
    print("✅ test_empty_candidates_rejected 通过")


# ---------- 接入 researcher 节点 ----------

def _run_researcher(fallback_name, primary_exc):
    from langchain_core.messages import AIMessage, HumanMessage
    from open_deep_research import deep_researcher as dr

    seen = []

    class Bound:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, messages):
            seen.append(self.name)
            if self.name == dr_primary and primary_exc:
                raise primary_exc
            return AIMessage(content=f"from {self.name}")

    dr_primary = "gpt-4.1"

    class FakeModel:
        def bind_tools(self, tools):
            return self

        def with_retry(self, **kw):
            return self

        def with_config(self, cfg):
            return Bound(cfg["model"].split(":")[-1])

    async def fake_tools(config):
        return [object()]

    orig = (dr.configurable_model, dr.get_all_tools)
    dr.configurable_model, dr.get_all_tools = FakeModel(), fake_tools
    cfg = {"configurable": {"research_model": "openai:gpt-4.1"}}
    if fallback_name:
        cfg["configurable"]["research_model_fallback"] = fallback_name
    try:
        out = asyncio.run(dr.researcher({"researcher_messages": [HumanMessage(content="q")]}, cfg))
    finally:
        dr.configurable_model, dr.get_all_tools = orig
    return seen, out.update["researcher_messages"][0].content


def test_researcher_default_path_unchanged_without_fallback_config():
    seen, content = _run_researcher(None, None)
    assert seen == ["gpt-4.1"] and content == "from gpt-4.1"
    print("✅ test_researcher_default_path_unchanged_without_fallback_config 通过")


def test_researcher_uses_fallback_only_after_worthy_primary_failure():
    seen, content = _run_researcher("openai:gpt-4.1-mini", None)
    assert seen == ["gpt-4.1"], "主模型成功时不应调用备用"
    seen, content = _run_researcher("openai:gpt-4.1-mini", openai.APIConnectionError(request=_REQ))
    assert seen == ["gpt-4.1", "gpt-4.1-mini"] and content == "from gpt-4.1-mini"
    print("✅ test_researcher_uses_fallback_only_after_worthy_primary_failure 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
