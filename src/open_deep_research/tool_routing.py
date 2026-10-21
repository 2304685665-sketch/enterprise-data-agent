"""tool_routing —— 按问题类型确定性地收回联网搜索工具（ODR_TOOL_ROUTING=1 才启用，默认关闭）。

动机（作者在模拟台账上的大规模真实模型运行）：
- 客商尽调：拿到 counterparty_due_diligence 的结果后，研究员仍按默认流程联网搜索一家内部快照里的公司，
  单题模型调用 50–130 次、费用约为普通题的 30 倍，且报告里出现"经查询法律数据库未发现诉讼"这类没有依据的陈述；
- 台账写请求（删除/修改/新增）：台账是只读的，模型仍联网搜"如何删除"并写一大段通用教程。
提示词/skill 层面的"不要联网"约束无效（V09/V10/V15 实测），所以改成工具层：命中规则的问题，研究员拿不到联网搜索工具。

规则是确定性的正则匹配，不是语义判断；没命中的问题完全不受影响（包括需要联网的混合问题）。
局限：问法绕开关键词（如"评估某公司作为供应商的风险"）时不会触发，仍会走默认流程。
"""
import os
import re

ENV_VAR = "ODR_TOOL_ROUTING"

_DILIGENCE = re.compile(r"尽调|尽职调查|客商|due\s*diligence|工商信息|工商快照", re.I)
_LEDGER_CTX = re.compile(r"台账|账期")
_WRITE_VERB = r"(?:删除|删掉|修改|改成|改为|更新|新增|添加|插入|标记为|标记成)"
_WRITE = re.compile(rf"{_WRITE_VERB}[^。\n]{{0,24}}(?:台账|账期|交易|记录|应收|库存|客户|供应商|逾期|款)"
                    rf"|(?:台账|账期|交易|记录|应收|库存)[^。\n]{{0,16}}{_WRITE_VERB}")
_WEB_NAMES = {"tavily_search", "web_search"}


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip() == "1"


_MARK = re.compile(r"\[路由:(diligence|ledger_write)\]")


def mark_for(original_text: str) -> str:
    """用户原始提问命中时返回标记串（供附在简报/研究任务末尾传给下游节点），否则空串。
    背景：研究员节点只看到主管改写后的任务书（常为英文、关键词丢失），所以要在
    拿得到用户原话的节点打标记，再随任务一路传下去。未启用时恒为空串。"""
    if not enabled():
        return ""
    r = block_reason(original_text or "")
    return f"\n[路由:{r}]" if r else ""


def block_reason(text: str):
    """命中返回原因字符串，否则 None。"""
    t = text or ""
    m = _MARK.search(t)
    if m:
        return m.group(1)
    if _DILIGENCE.search(t):
        return "diligence"
    if _LEDGER_CTX.search(t) and _WRITE.search(t):
        return "ledger_write"
    return None


def _is_web_tool(tool) -> bool:
    if isinstance(tool, dict):
        return str(tool.get("type", "")).startswith("web_search") or tool.get("name") in _WEB_NAMES
    return getattr(tool, "name", None) in _WEB_NAMES


def filter_tools(tools, topic_text: str):
    """未启用或未命中：原样返回同一个列表；命中：去掉联网搜索工具（其余工具不动）。"""
    if not enabled() or block_reason(topic_text) is None:
        return tools
    return [t for t in tools if not _is_web_tool(t)]
