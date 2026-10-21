"""citation_verification.py —— 离线核验最终报告里引用的URL是不是真的在这次
研究过程中被检索到过（fetched_sources 来自 researcher_tools 对 ToolMessage
内容的结构化解析，见 state.py 的 FETCHED_SOURCES_DOC）。

设计原则：
- 纯离线、纯字符串/数据处理，不发任何网络请求，不调用任何模型API。
- 只标记，不修改/不删除 final_report 里的任何内容——这是只读分析工具，
  调用方自己决定拿到结果之后要怎么用。
- URL 归一化用于比较，不是"猜测两个URL是不是同一个东西"的模糊匹配——
  只做明确、可解释的归一化规则（见 normalize_url），归一化不掉的差异就
  判定为不同URL，不做启发式近似匹配。
"""

import re
from urllib.parse import urlsplit, urlunsplit

_URL_RE = re.compile(r"https?://\S+")

# URL 末尾常见的标点，在正文里URL后面经常紧跟着这些符号（句号、右括号、
# 逗号等），属于正文标点不属于URL本身，归一化时需要剥掉，否则会把
# "https://x.com." 和 "https://x.com" 误判成不同URL。
_TRAILING_PUNCTUATION = ".,;:)]}>\"'"


def normalize_url(url: str) -> str:
    """明确规则的URL归一化，不是模糊匹配：
    - 剥掉URL末尾可能混入的正文标点。
    - scheme/host 转小写（host本身大小写不敏感，scheme同理）；path/query
      保持原样（这两部分可能是大小写敏感的，不能一律转小写）。
    - 去掉默认端口（http:80 / https:443）。
    - 去掉末尾的单个 "/"（但根路径"/"本身保留，避免把 "https://x.com"
      和 "https://x.com/" 之外的情况也错误合并）。
    """
    url = url.rstrip(_TRAILING_PUNCTUATION)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, parts.query, ""))  # fragment丢弃，不参与比较


def extract_cited_urls(final_report_text: str) -> list[str]:
    """从最终报告正文里找出所有看起来像URL的子串（http/https开头，直到
    空白字符为止），不区分是不是在"Sources"小节里——不同模型/不同报告的
    来源小节格式不统一，直接扫全文找URL比依赖某个固定小节标题更稳健。
    返回的是归一化之后的URL列表，可能含重复（重复由调用方按需处理）。
    """
    return [normalize_url(m) for m in _URL_RE.findall(final_report_text)]


def verify_citations(final_report_text: str, fetched_sources: list[dict]) -> dict:
    """核心校验函数。返回结构：
    {
        "valid": [{"url": ..., "source": ..., "research_topic": ...}, ...],   # 报告里引用的URL，确实在fetched_sources里出现过
        "invalid": [url, ...],                                                # 报告里引用的URL，fetched_sources里完全没有——可能是模型编的
        "missing": [{"url": ..., "source": ..., "research_topic": ...}, ...], # fetched_sources里真实检索到过，但报告正文完全没提到
    }
    只标记，不改final_report_text本身。
    """
    cited_urls = set(extract_cited_urls(final_report_text))

    normalized_fetched = {}  # normalized_url -> 原始fetched_sources条目（取第一次出现的）
    for entry in fetched_sources:
        norm = normalize_url(entry.get("url", ""))
        if norm and norm not in normalized_fetched:
            normalized_fetched[norm] = entry

    valid = []
    invalid = []
    for url in cited_urls:
        if url in normalized_fetched:
            entry = normalized_fetched[url]
            valid.append({
                "url": url,
                "source": entry.get("source"),
                "research_topic": entry.get("research_topic"),
            })
        else:
            invalid.append(url)

    missing = [
        {"url": norm_url, "source": entry.get("source"), "research_topic": entry.get("research_topic")}
        for norm_url, entry in normalized_fetched.items()
        if norm_url not in cited_urls
    ]

    return {"valid": valid, "invalid": invalid, "missing": missing}


# ---------------------------------------------------------------------------
# T3b：内部文档（internal_doc_search）引用的可追溯性核验。
#
# 与上面的 URL 核验是两条独立的证据链，刻意不共享代码路径：URL 有通用词法
# 形式（https?://...），doc_id 没有——这里只能依赖两样东西：
#   (1) internal_doc_search 自己输出的固定格式（代码生成、结构稳定）；
#   (2) 报告里模型可能保留下来的 [Source: ...] 标记，以及知识库文件命名约定。
# (2) 的可靠性明显弱于 URL 提取，局限见 verify_doc_id_citations 的文档字符串。
# ---------------------------------------------------------------------------

_INTERNAL_DOC_CHUNK_SEPARATOR = "\n\n---\n\n"

# internal_doc_search.py 里每个 chunk 的首行格式（f-string 原样）：
#   [Source: {doc_id}, section: {section}, relevance_distance: {distance:.3f}]
_INTERNAL_DOC_HEADER_RE = re.compile(
    r"^\[Source: (?P<doc_id>[^,\]\s]+), section: .*, relevance_distance: -?\d+(?:\.\d+)?\]$"
)

# 报告里形如 [Source: <id>, ...] 或 [Source: <id>] 的标记。id 只允许字母数字、
# 下划线、连字符，可带 .md 后缀；"[Source: https://..."、"[Source: example.com]"、
# "[Source: Company Handbook]" 都不会匹配，避免把 URL 引用或普通文字误判成 doc_id。
_REPORT_SOURCE_MARKER_RE = re.compile(
    r"\[Source:\s*([A-Za-z0-9][A-Za-z0-9_\-]*)(?:\.md)?\s*[,\]]", re.IGNORECASE
)

# 次级启发式：依赖当前知识库文件命名约定（docNN_description），这个约定不是
# 任何代码强制的 schema，命名方式变化后这条规则会失效或误报。
_DOC_ID_NAMING_HEURISTIC_RE = re.compile(r"\bdoc\d{2}_[A-Za-z0-9_]+\b", re.IGNORECASE)


def extract_internal_doc_ids(tool_output: str) -> list[str]:
    """从一次 internal_doc_search 的返回文本里提取 doc_id（去重、保持首次出现顺序）。

    只按 chunk 分隔符切分后解析每个 chunk 的**首行**，不解析 chunk 正文——
    知识库文档正文里完全可能出现形如 "[Source: ...]" 的文字，那不是检索记录。
    "No relevant documents found..." 之类没有 chunk 首行的返回，结果为空。
    调用方负责保证 tool_output 来自 internal_doc_search 的成功返回。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for chunk in tool_output.split(_INTERNAL_DOC_CHUNK_SEPARATOR):
        first_line = chunk.split("\n", 1)[0].strip()
        m = _INTERNAL_DOC_HEADER_RE.match(first_line)
        if not m:
            continue
        doc_id = m.group("doc_id")
        if doc_id not in seen:
            seen.add(doc_id)
            ids.append(doc_id)
    return ids


def _normalize_doc_id(doc_id: str) -> str:
    doc_id = doc_id.strip().lower()
    if doc_id.endswith(".md"):
        doc_id = doc_id[: -len(".md")]
    return doc_id


def extract_cited_doc_ids(final_report_text: str) -> list[str]:
    """从报告正文里找"看起来像内部文档 doc_id"的引用（归一化为小写、去掉 .md），
    合并两条规则、去重、保持首次出现顺序：
      (a) 基于格式：[Source: <id>, ...] / [Source: <id>] 标记；
      (b) 次级启发式：docNN_xxx 命名约定（依赖当前知识库命名，见上方说明）。
    """
    found: list[str] = []
    seen: set[str] = set()
    for rx in (_REPORT_SOURCE_MARKER_RE, _DOC_ID_NAMING_HEURISTIC_RE):
        for m in rx.finditer(final_report_text):
            raw = m.group(1) if rx is _REPORT_SOURCE_MARKER_RE else m.group(0)
            norm = _normalize_doc_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                found.append(norm)
    return found


def verify_doc_id_citations(final_report_text: str, fetched_doc_ids: list[dict]) -> dict:
    """返回：
    {
        "valid":   [doc_id, ...],  # 报告里引用的 doc_id，本次确实被 internal_doc_search 检索到过
        "invalid": [doc_id, ...],  # 报告里引用的 doc_id，本次检索记录里没有
        "missing": [doc_id, ...],  # 检索到过，但报告正文完全没出现（仅在返回值里，不写进报告）
    }
    只标记，不改报告。

    边界（必须随结果一起理解）：
    - 只证明“被引用的 doc_id 在本次研究过程中确实被检索到过”，不证明模型对文档
      内容的转述准确，也不证明文档内容支持报告里的结论。
    - invalid 的可靠性弱于 URL 核验：模型改写、截断、翻译 doc_id 后的引用检测不到；
      次级启发式依赖 docNN_ 命名约定。
    - missing 用“已知 doc_id 字符串是否出现在报告里（不区分大小写）”判断，
      不依赖上面的启发式。
    """
    fetched_norm: dict[str, str] = {}
    for entry in fetched_doc_ids:
        norm = _normalize_doc_id(entry.get("doc_id", ""))
        if norm and norm not in fetched_norm:
            fetched_norm[norm] = entry.get("doc_id", "")

    cited = extract_cited_doc_ids(final_report_text)
    valid = [c for c in cited if c in fetched_norm]
    invalid = [c for c in cited if c not in fetched_norm]

    lowered_report = final_report_text.lower()
    missing = [orig for norm, orig in fetched_norm.items() if norm not in lowered_report]
    return {"valid": valid, "invalid": invalid, "missing": missing}
