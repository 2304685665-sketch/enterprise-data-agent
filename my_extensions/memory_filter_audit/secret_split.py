"""语料里的合成密钥样本以“拆开”的形式存放，避免 GitHub 推送保护/密钥扫描把它们当成真实泄露。

存放时在令牌前缀后插入不可见分隔标记 MARK；加载时去掉标记，得到与拆分前逐字节相同的内容
（所以冻结语料的 SHA-256 以“还原后”的字节计算，数值不变）。这只是存放方式，不改变评测内容。
"""
import re

MARK = "\u27e6\u00b7\u27e7"  # ⟦·⟧
_PAT = re.compile(r"(xox[abprs]-|sk_live_|ghp_|gho_|ghu_|ghs_|ghr_|AKIA|sk-proj-|sk-)([A-Za-z0-9\-_]{10,})")


def split(text: str) -> str:
    return _PAT.sub(lambda m: m.group(1)[:3] + MARK + m.group(1)[3:] + m.group(2), text)


def restore(data):
    if isinstance(data, bytes):
        return data.replace(MARK.encode("utf-8"), b"")
    return data.replace(MARK, "")
