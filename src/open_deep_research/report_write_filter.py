"""报告后处理：写请求场景下，确定性地移除报告里的“可直接执行的写语句”。

背景：提示词（EVIDENCE §32）只能影响措辞，留出集 3 上仍有 3/8（V1）的写请求报告含 INSERT/UPDATE/DELETE/DROP。
这里不再依赖模型遵守，而是在最终报告生成之后做规则过滤。

边界（诚实）：
- 只在“用户消息被识别为写请求”（write_intent.detect_write_request）时启用，非写请求的报告一字不改。
- 默认关闭，ODR_WRITE_SQL_FILTER=1 才启用（先用实验证明，再决定默认值）。
- 规则型：含写语句的围栏代码块整块移除（同一代码块里的只读 SELECT 也会一并丢失，这是已知代价）；
  行内 `...` 含写语句的片段替换为占位；其余行里出现写语句关键字的整行替换为占位。
- 不含 CREATE TABLE：报告展示表结构（DDL）是只读信息，早期版本把它算作写语句会误伤大量只读报告（离线检验发现，已修正）。
- 它只清理报告里的“写语句文本”，不会修正报告里“修改已发生”之类的事实性错误（那是另一个缺陷，见 §32.2 h2-write-prune）。
- 这个指标（报告里是否含写语句）对该过滤是“构造性”的：过滤后几乎必然为 0，只能说明机制有效，不能当泛化证据；
  真正需要检验的是误伤（只读报告被改动、只读 SQL 被误删）和端到端通过数。
"""
from __future__ import annotations

import os
import re

PLACEHOLDER = "（此处原有写操作语句已被系统移除：本系统只读，未执行任何修改。）"
WRITE_RE = re.compile(
    r"\b(drop\s+(table|index|view|trigger)|truncate\s+table|delete\s+from|insert\s+(or\s+\w+\s+)?into|"
    r"replace\s+into|update\s+[\w\"`\[\]\.]+\s+set|alter\s+table)\b",
    re.I,
)
_FENCE = re.compile(r"```[^\n]*\n.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]+`")


def enabled() -> bool:
    return os.environ.get("ODR_WRITE_SQL_FILTER", "0") == "1"


def strip_write_sql(report: str) -> tuple[str, int]:
    """返回 (处理后的文本, 被替换的片段数)。无写语句时原样返回。"""
    if not report or not WRITE_RE.search(report):
        return report, 0
    n = 0

    def fence(m: re.Match) -> str:
        nonlocal n
        if WRITE_RE.search(m.group(0)):
            n += 1
            return PLACEHOLDER
        return m.group(0)

    def inline(m: re.Match) -> str:
        nonlocal n
        if WRITE_RE.search(m.group(0)):
            n += 1
            return PLACEHOLDER
        return m.group(0)

    out = _FENCE.sub(fence, report)
    out = _INLINE.sub(inline, out)
    lines = []
    for line in out.split("\n"):
        if WRITE_RE.search(line):
            n += 1
            lines.append(PLACEHOLDER)
        else:
            lines.append(line)
    return "\n".join(lines), n


def filter_report_for_write_request(report: str, is_write_request: bool) -> str:
    if not (enabled() and is_write_request) or not isinstance(report, str):
        return report
    return strip_write_sql(report)[0]
