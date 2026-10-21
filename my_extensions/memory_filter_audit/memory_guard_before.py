"""长期记忆的写入/读取准入过滤（规则型，确定性）。

为什么需要：memory_manager 把用户原始提问整段交给 mem0 保存，读取时又把保存的条目直接拼进
研究简报的提示词。这构成两条风险链：
  1) 写入：含密钥/口令/证件号/银行卡号的内容被长期保存；
  2) 读取：被"植入"过的指令式文本（"忽略之前的指令……"）会在以后每次对话里被重新注入提示词。
准入过滤在两端各拦一道：写入前不让敏感或指令式内容进库，读取时丢掉不合格的存量条目并截断过长条目。

边界（诚实声明）：这是正则规则，不是语义理解。能拦已知形态（见 test_memory_guard 的攻击/正常用例集），
拦不住换了说法的注入；也会误拦"恰好在讨论提示词注入"的正常提问（宁可不记住，也不冒险）。
默认关闭，环境变量 MEMORY_GUARD=1 启用。
"""

from __future__ import annotations

import os
import re
from typing import Iterable

GUARD_ENV_VAR = "MEMORY_GUARD"
MAX_WRITE_CHARS = 2000
MAX_READ_ITEM_CHARS = 300

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)",
    r"disregard\s+.{0,30}(instructions?|rules?|prompts?)",
    r"forget\s+(all\s+|everything\s+)?(your\s+|the\s+)?(previous|prior)?\s*(instructions?|rules?)",
    r"you\s+are\s+now\s+(a|an|in)\b",
    r"(reveal|print|show|leak|output)\s+.{0,20}system\s+prompt",
    r"</?\s*(system|assistant|developer)\s*>",
    r"忽略(以上|之前|前面|上述|先前)?(所有|全部)?(的)?(指令|提示|规则|说明|要求)",
    r"无视.{0,8}(指令|规则|提示)",
    r"(泄露|输出|打印|透露).{0,8}(系统提示|提示词|system prompt)",
    r"(系统提示|提示词|system prompt).{0,8}(泄露|透露|告诉我|发给我|输出|打印)",
    r"从现在起.{0,10}(你是|你将|扮演)",
]
_SECRET_PATTERNS = [
    r"\bsk-[A-Za-z0-9_\-]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bBearer\s+[A-Za-z0-9._\-]{20,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(password|passwd|pwd|api[_\-]?key|secret|access[_\-]?token)\s*[:=：]\s*\S{6,}",
    r"(密码|口令|密钥)\s*(是|为|[:=：])\s*\S{6,}",
]
_INJECTION_RE = [re.compile(p, re.I) for p in _INJECTION_PATTERNS]
_SECRET_RE = [re.compile(p) for p in _SECRET_PATTERNS]
_CN_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


def guard_enabled() -> bool:
    return os.environ.get(GUARD_ENV_VAR, "").strip() == "1"


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _has_card_number(text: str) -> bool:
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def contains_injection(text: str) -> bool:
    return any(r.search(text) for r in _INJECTION_RE)


def contains_sensitive(text: str) -> bool:
    return any(r.search(text) for r in _SECRET_RE) or bool(_CN_ID_RE.search(text)) or _has_card_number(text)


def admit_write(text: str) -> tuple[bool, str]:
    """返回 (是否允许写入, 原因)。原因 'ok' 表示通过。"""
    if not isinstance(text, str) or not text.strip():
        return False, "empty"
    if len(text) > MAX_WRITE_CHARS:
        return False, "too_long"
    if contains_injection(text):
        return False, "instruction_like"
    if contains_sensitive(text):
        return False, "sensitive"
    return True, "ok"


def filter_read(items: Iterable[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """过滤存量记忆。items 为 mem0 返回的字典列表（条目文本在 'memory' 键）。
    返回 (保留的条目, [(被丢弃条目文本前 40 字, 原因)])。保留的过长条目会被截断。"""
    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []
    for it in items:
        text = it.get("memory", "") if isinstance(it, dict) else ""
        if not isinstance(text, str) or not text.strip():
            dropped.append(("", "empty"))
            continue
        if contains_injection(text):
            dropped.append((text[:40], "instruction_like"))
            continue
        if contains_sensitive(text):
            dropped.append((text[:40], "sensitive"))
            continue
        if len(text) > MAX_READ_ITEM_CHARS:
            it = {**it, "memory": text[:MAX_READ_ITEM_CHARS] + "…"}
        kept.append(it)
    return kept, dropped
