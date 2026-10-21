"""DEEP_RESEARCH_GROUNDING_GUARD 的离线测试：默认关闭时发给报告模型的提示词与改动前逐字节一致；
开启时追加数据依据约束。假模型，不调用任何 API。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-offline-grounding-test")

from langchain_core.messages import AIMessage  # noqa: E402

from open_deep_research import deep_researcher as dr  # noqa: E402

ENV = dr.GROUNDING_GUARD_ENV_VAR


class _Capture:
    def __init__(self):
        self.prompts = []

    def with_config(self, _cfg):
        return self

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        return AIMessage(content="FAKE REPORT")


def _run_report():
    cap = _Capture()
    old = dr.configurable_model
    dr.configurable_model = cap
    try:
        state = {"messages": [], "research_brief": "brief", "notes": ["NOTE: sql_executor error, no data"],
                 "degradation_events": []}
        out = asyncio.run(dr.final_report_generation(state, {"configurable": {}}))
    finally:
        dr.configurable_model = old
    assert out["final_report"].startswith("FAKE REPORT")
    return cap.prompts[0]


def test_default_off_prompt_is_unchanged():
    os.environ.pop(ENV, None)
    assert dr._build_grounding_prompt_hint() == ""
    p = _run_report()
    assert dr.GROUNDING_GUARD_MARKER not in p
    expected = dr.final_report_generation_prompt.format(
        research_brief="brief", messages="", findings="NOTE: sql_executor error, no data", date=dr.get_today_str())
    assert p == expected, "关闭时提示词必须与改动前逐字节一致"
    for v in ("0", "false", "", "no"):
        os.environ[ENV] = v
        assert dr._build_grounding_prompt_hint() == ""
    os.environ.pop(ENV, None)
    print("✅ test_default_off_prompt_is_unchanged 通过")


def test_enabled_appends_guard_after_findings():
    for v in ("1", "true", "YES", "on"):
        os.environ[ENV] = v
        try:
            p = _run_report()
        finally:
            os.environ.pop(ENV, None)
        assert dr.GROUNDING_GUARD_MARKER in p and "未取得该数据" in p and "NOTE: sql_executor error" in p
        assert p.index("NOTE: sql_executor error") < p.index(dr.GROUNDING_GUARD_MARKER)
    print("✅ test_enabled_appends_guard_after_findings 通过")


if __name__ == "__main__":
    test_default_off_prompt_is_unchanged()
    test_enabled_appends_guard_after_findings()
    print("\n全部测试通过！")
