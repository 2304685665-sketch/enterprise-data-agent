"""写请求识别（确定性规则，不调用模型）。

背景：评测发现“请把X改成Y / 删除Z / 新增W”这类请求会在“研究简报改写”一步被改写成纯查询任务，
最终报告提示词看不到写意图，于是报告里出现“建议执行 DROP TABLE”或把“翻倍后的金额”当真实数据列出（EVIDENCE §32）。
这里在最终报告阶段按用户原话再识别一次，命中时给报告模型一条明确的只读声明要求。

边界（诚实）：
- 这是规则匹配，会漏掉没见过的说法，也会误触发（例如“请更新一下对X行业的研究”）；
  误触发的代价只是报告开头多一句“本系统只读”的说明，所以规则偏宽松，但要求“祈使句式 + 写动作”，
  不会因为单独出现“新增/更新”（如“2026 年新增了几笔融资”）而命中。
- 它只约束报告措辞，不是权限控制：真正的写保护由只读连接 + SQL 守卫 + 审批策略负责。
- 环境变量 ODR_WRITE_REQUEST_HINT=0 可关闭。
"""
from __future__ import annotations

import os
import re
from typing import Iterable

_ZH_VERBS = r"(改成|改为|修改|更改|更新|设为|设置为|置为|调整为|删除|删掉|清空|清除|新增|添加|增加|插入|写入|翻一倍|翻倍)"
_ZH = [
    re.compile(r"[把将][^，。！？\n]{1,60}?" + _ZH_VERBS),                    # 把X改成Y / 将X删除
    re.compile(r"给[^，。！？\n]{1,30}?(添加|新增|增加|插入|写入)"),                # 给X添加一轮融资
    re.compile(r"(请|麻烦|帮我|请你|帮忙)[^，。！？\n]{0,12}?" + _ZH_VERBS),         # 请清空 / 请删除 / 帮我修改
]
_SQL = re.compile(r"\b(drop\s+table|truncate\s+table|delete\s+from|insert\s+into|update\s+\w+\s+set|alter\s+table)\b", re.I)
_EN = [
    re.compile(r"\b(update|change|modify|set)\b[^.?!\n]{0,60}\bto\b", re.I),
    re.compile(r"\b(delete|remove|drop|truncate|wipe)\b[^.?!\n]{0,80}\b(table|rows?|records?|rounds?|entries|database|data)\b", re.I),  # 40→80、加 rounds：2026-10-13 看到 h3-write-en-delete 漏检后放宽（事后修改，见 EVIDENCE §36）
    re.compile(r"\b(insert|add)\b[^.?!\n]{0,40}\b(record|row|round|entry|company)\b", re.I),
]


def detect_write_request(text: str) -> bool:
    if not text:
        return False
    return bool(_SQL.search(text) or any(p.search(text) for p in _ZH) or any(p.search(text) for p in _EN))


HINT_V1 = (
    "\n\n[SYSTEM NOTICE: DATA-MODIFICATION REQUEST DETECTED] The user's message asks to modify, delete or insert data. "
    "The database tools available in this system are read-only: no change has been made and none can be made. "
    "The first two sentences of the report MUST state that you cannot perform this change and that no data was modified. "
    "You may then report the current state exactly as read from the database. Do not present hypothetical post-change "
    "values as if they were real data, and do not offer a ready-to-run write statement as the recommended next step."
)

# V2（默认不启用，ODR_WRITE_HINT_VERSION=2 才用；EVIDENCE §32.3）：留出集 2 上 V1 仍有两类问题——报告把“用户想要的修改后状态”当成已发生的事实
# （“2025 年前的记录已被删除”），以及报告里附带可直接执行的写语句。V2 把这两点写成明确禁令。
HINT_V2 = HINT_V1 + (
    " Additional rules: (1) Never state or imply that the requested change has already happened, is already in effect, "
    "or is company policy, even if the user's wording presupposes it; the database still contains exactly what the query "
    "results show, so describe those rows as still present. (2) If you give a count or total, say it is the current "
    "count in the database and that it was not produced by the requested change. (3) Do not include INSERT, UPDATE, "
    "DELETE, DROP, TRUNCATE or ALTER statements anywhere in the report, not even as an example or a suggestion; "
    "say instead that the change must be made by a person with write access."
)


def _hint_text() -> str:
    return HINT_V2 if os.environ.get("ODR_WRITE_HINT_VERSION", "1") == "2" else HINT_V1


HINT = HINT_V1  # 默认 V1：留出集 3 上 V2 没有比 V1 好（EVIDENCE §32.3）


def _human_texts(messages: Iterable) -> list[str]:
    out = []
    for m in messages or []:
        if getattr(m, "type", None) == "human":
            c = getattr(m, "content", "")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
            out.append(str(c))
    return out


def build_write_request_hint(messages: Iterable) -> str:
    if os.environ.get("ODR_WRITE_REQUEST_HINT", "1") == "0":
        return ""
    return _hint_text() if any(detect_write_request(t) for t in _human_texts(messages)) else ""
