"""tool_routing：规则命中表、工具过滤、默认关闭、接入 researcher 节点（假模型）。无任何模型调用。
用法：PYTHONPATH=src python my_extensions/test_tool_routing.py"""
import asyncio, os
from open_deep_research import tool_routing as tr

POS = {
    "请对示例乙矿业贸易有限公司做客商尽调，有哪些风险信号？": "diligence",
    "Please run due diligence on Acme Metals": "diligence",
    "对这家公司做尽职调查": "diligence",
    "请把内部台账里供应商03的账期改成20天。": "ledger_write",
    "请删除内部台账里2026年1月的全部交易记录。": "ledger_write",
    "请在内部台账里新增一笔铜的销售交易，客户05，10吨": "ledger_write",
    "请把内部台账里所有逾期未收款标记为已收。": "ledger_write",
    "把台账中客户12的账期修改为75天": "ledger_write",
}
NEG = [  # 必须不触发：只读台账问题、普通联网问题、与台账无关的"更新/删除"
    "内部台账里客户07目前有几笔逾期未收款，合计多少钱？",
    "内部台账里2026年第二季度的销售额占上半年的百分比是多少？",
    "最近一周全球铜价走势有什么值得关注的新闻？",
    "怎么删除手机里的缓存文件",
    "请基于内部台账给我一份2026年6月的月度台账报告",
    "",
]


def test_rules():
    for q, why in POS.items():
        assert tr.block_reason(q) == why, (q, tr.block_reason(q))
    for q in NEG:
        assert tr.block_reason(q) is None, (q, tr.block_reason(q))
    # 已知局限：绕开关键词的问法不触发（如实记录，而不是假装能抓住）
    assert tr.block_reason("评估一下示例丙铜业股份有限公司作为供应商的风险") is None
    print("✅ 8 个应触发、6 个不应触发的问法判断正确；已知绕开问法如实不触发")


class _T:  # 假 LangChain 工具
    def __init__(self, name): self.name = name


def test_filter_and_default_off():
    tools = [_T("tavily_search"), _T("sql_executor"), {"type": "web_search_preview"}, {"type": "web_search_20250305", "name": "web_search"}, _T("think_tool")]
    os.environ.pop(tr.ENV_VAR, None)
    assert tr.filter_tools(tools, "请对某公司做客商尽调") is tools, "关闭时必须原样返回同一个列表"
    os.environ[tr.ENV_VAR] = "1"
    try:
        kept = tr.filter_tools(tools, "请对某公司做客商尽调")
        assert [getattr(t, "name", t) for t in kept] == ["sql_executor", "think_tool"], kept
        assert tr.filter_tools(tools, "最近铜价新闻") is tools, "未命中时必须原样返回"
    finally:
        os.environ.pop(tr.ENV_VAR, None)
    print("✅ 关闭或未命中时原样返回；命中时只去掉联网搜索工具（含 OpenAI/Anthropic 原生搜索）")


def _bound_names(topic, on):
    from langchain_core.messages import AIMessage, HumanMessage
    from open_deep_research import deep_researcher as dr
    seen = {}

    class Bound:
        async def ainvoke(self, messages): return AIMessage(content="ok")

    class Model:
        def bind_tools(self, tools):
            seen["names"] = [getattr(t, "name", None) or t.get("name") for t in tools]; return self
        def with_retry(self, **kw): return self
        def with_config(self, cfg): return Bound()

    async def fake_tools(config): return [_T("tavily_search"), _T("sql_executor"), _T("counterparty_due_diligence")]

    orig = (dr.configurable_model, dr.get_all_tools); dr.configurable_model, dr.get_all_tools = Model(), fake_tools
    (os.environ.__setitem__ if on else os.environ.pop)(*((tr.ENV_VAR, "1") if on else (tr.ENV_VAR, None)))
    try:
        asyncio.run(dr.researcher({"researcher_messages": [HumanMessage(content=topic)]}, {"configurable": {}}))
    finally:
        dr.configurable_model, dr.get_all_tools = orig; os.environ.pop(tr.ENV_VAR, None)
    return seen["names"]


def test_researcher_node_integration():
    assert "tavily_search" in _bound_names("请对示例乙矿业贸易有限公司做客商尽调", on=False)
    assert "tavily_search" not in _bound_names("请对示例乙矿业贸易有限公司做客商尽调", on=True)
    assert "tavily_search" not in _bound_names("请把内部台账里供应商03的账期改成20天", on=True)
    assert "tavily_search" in _bound_names("最近一周全球铜价走势有什么新闻", on=True)
    assert {"sql_executor", "counterparty_due_diligence"} <= set(_bound_names("对某公司做客商尽调", on=True))
    print("✅ researcher 节点：开关关闭或未命中保留联网工具；命中时绑定到模型的工具里没有联网搜索，其余工具保留")


if __name__ == "__main__":
    test_rules(); test_filter_and_default_off(); test_researcher_node_integration(); print("ALL PASSED")


# ---- 追加：标记传递（主管改写成英文后仍能路由）----
def _test_mark():
    import os
    from open_deep_research import tool_routing as tr
    os.environ.pop(tr.ENV_VAR, None)
    assert tr.mark_for("请对示例戊化工有限公司做客商尽调") == ""          # 默认关闭
    os.environ[tr.ENV_VAR] = "1"
    m = tr.mark_for("请对示例戊化工有限公司做客商尽调")
    assert m == "\n[路由:diligence]"
    assert tr.mark_for("请把供应商03的账期改成20天") == "\n[路由:ledger_write]"
    assert tr.mark_for("最近铜价新闻") == ""
    english = "I need step-by-step instructions to change terms for supplier 03." + "\n[路由:ledger_write]"
    assert tr.block_reason(english) == "ledger_write"                    # 英文改写+标记仍命中
    assert tr.block_reason("I need step-by-step instructions.") is None
    class T: name = "tavily_search"
    class U: name = "sql_executor"
    assert [t.name for t in tr.filter_tools([T(), U()], english)] == ["sql_executor"]
    os.environ.pop(tr.ENV_VAR, None)
    print("✅ 标记：默认关闭为空；命中打标；英文改写+标记仍去掉联网工具")

_test_mark()
