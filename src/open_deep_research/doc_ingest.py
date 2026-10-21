"""doc_ingest.py —— 知识库文档解析、清洗与切分（Markdown / PDF）。

只依赖已在 pyproject 里的 pymupdf；不联网、不调用任何模型/Embedding API。
本模块只负责"文件 -> 干净的、按标题切好的文本块"，不写入向量库（写入需要
Embedding API，由 build_knowledge_base 一类脚本在获批后负责）。

能力边界（如实）：
- PDF：只处理"有文字层"的 PDF。扫描件/纯图片 PDF 没有文字层，本模块**不做 OCR**，
  会返回 has_text=False 并给出 warning，调用方应当告知用户而不是静默入库空内容。
- 标题识别是启发式：字号明显大于正文的短行视为标题（按字号占比，不看加粗/样式），
  复杂排版（多栏、表格、页内小标题与正文同字号）可能识别不准。
- 页眉页脚：同一规范化文本（数字替换为 #）出现在 >=50% 页面（且页数 >=3）、位于
  页面上/下边缘区域，才会被删除；页数很少的文档不做该删除，避免误删正文。
- 清洗只做保守的规范化（NFKC、去控制字符、合并多余空白、去连续重复行、去纯页码行），
  不改写语义内容。
"""

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

_PAGE_NUMBER_RE = re.compile(r"^\s*(第\s*\d+\s*页(\s*/\s*共\s*\d+\s*页)?|[-—–\s]*\d+[-—–\s]*|page\s*\d+(\s*of\s*\d+)?)\s*$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EDGE_FRACTION = 0.08  # 页面上/下 8% 视为页眉页脚区域
_HEADING_SIZE_RATIO = 1.2
_HEADING_MAX_LEN = 60


@dataclass
class ParsedDoc:
    text: str
    pages: int
    has_text: bool
    warnings: list[str] = field(default_factory=list)


def clean_text(text: str, pdf_artifacts: bool = False) -> str:
    """保守清洗。始终做：NFC 规范化（不用 NFKC——NFKC 会把中文全角标点改成半角，
    改变文本内容）、去控制字符、行内多空白合并、折叠 3 个以上连续空行。
    仅当 pdf_artifacts=True（PDF 解析结果）才额外：去纯页码行、去连续重复行——
    这两条对 Markdown 正文可能误删（比如单独成行的年份），所以不对 md 启用。"""
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_RE.sub("", text)
    out: list[str] = []
    prev = None
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if pdf_artifacts:
            if _PAGE_NUMBER_RE.match(line):
                continue
            if line and line == prev:
                continue
        out.append(line)
        if line:
            prev = line
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _norm_for_repeat(s: str) -> str:
    return re.sub(r"\d+", "#", unicodedata.normalize("NFKC", s)).strip().lower()


def parse_pdf(source) -> ParsedDoc:
    """source：文件路径或 bytes。返回带 '## 标题' 行的 Markdown 风格文本。"""
    doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, (bytes, bytearray)) else pymupdf.open(source)
    try:
        pages_lines: list[list[dict]] = []
        for page in doc:
            height = page.rect.height or 1.0
            lines = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for ln in block.get("lines", []):
                    spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in spans).strip()
                    size = max(s["size"] for s in spans)
                    y0, y1 = ln["bbox"][1], ln["bbox"][3]
                    edge = (y1 / height) <= _EDGE_FRACTION or (y0 / height) >= (1 - _EDGE_FRACTION)
                    lines.append({"text": text, "size": size, "edge": edge, "y0": y0})
            lines.sort(key=lambda d: d["y0"])
            pages_lines.append(lines)
        n_pages = len(pages_lines)
        all_lines = [d for p in pages_lines for d in p]
        if not all_lines:
            return ParsedDoc("", n_pages, False,
                             ["PDF 没有可提取的文字层（可能是扫描件）；本模块不做 OCR。"])

        # 页眉页脚：边缘区域、跨页重复
        drop: set[str] = set()
        if n_pages >= 3:
            counts: Counter = Counter()
            for p in pages_lines:
                for key in {_norm_for_repeat(d["text"]) for d in p if d["edge"]}:
                    counts[key] += 1
            drop = {k for k, c in counts.items() if c / n_pages >= 0.5}

        # 正文字号：按字符数加权的众数
        size_chars: Counter = Counter()
        for d in all_lines:
            size_chars[round(d["size"], 1)] += len(d["text"])
        body_size = size_chars.most_common(1)[0][0]

        out: list[str] = []
        for p in pages_lines:
            for d in p:
                if d["edge"] and _norm_for_repeat(d["text"]) in drop:
                    continue
                t = d["text"]
                is_heading = (d["size"] >= body_size * _HEADING_SIZE_RATIO
                              and len(t) <= _HEADING_MAX_LEN and not t.endswith(("。", ".", "；", ";")))
                out.append(f"## {t}" if is_heading else t)
        text = clean_text("\n".join(out), pdf_artifacts=True)
        return ParsedDoc(text, n_pages, bool(text), [] if text else ["清洗后没有剩余文本。"])
    finally:
        doc.close()


def split_into_chunks(text: str, doc_id: str, max_chars: int = 800) -> list[dict]:
    """按 '## ' 标题切分（与 build_knowledge_base 相同的规则：第一个标题之前的内容丢弃）；
    超过 max_chars 的段落再按空行/句号边界切成多段，section 名带序号后缀。"""
    chunks: list[dict] = []
    title, buf = None, []

    def flush():
        if title is None:
            return
        body = "\n".join(buf).strip()
        if not body:
            return
        for i, piece in enumerate(_split_long(body, max_chars)):
            chunks.append({"doc_id": doc_id, "section": title if i == 0 else f"{title} ({i + 1})", "text": piece})

    for line in text.split("\n"):
        if line.startswith("## "):
            flush()
            title, buf = line[3:].strip(), [line]
        else:
            buf.append(line)
    flush()
    return chunks


def _split_long(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    pieces, cur = [], ""
    for para in re.split(r"\n\s*\n|(?<=[。！？.!?])\s*", body):
        if not para:
            continue
        if cur and len(cur) + len(para) + 1 > max_chars:
            pieces.append(cur.strip())
            cur = ""
        while len(para) > max_chars:  # 单个超长片段硬切
            pieces.append(para[:max_chars])
            para = para[max_chars:]
        cur = (cur + "\n" + para) if cur else para
    if cur.strip():
        pieces.append(cur.strip())
    return pieces


def ingest_file(path: str | Path, max_chars: int = 800) -> tuple[list[dict], list[str]]:
    """按扩展名解析单个文件，返回 (chunks, warnings)。chunk 带 id、doc_id、section、text、source_type。
    id = {doc_id}_chunk{i}；doc_id = 文件名去扩展名。不支持的扩展名抛 ValueError。"""
    p = Path(path)
    doc_id = p.stem
    ext = p.suffix.lower()
    if ext == ".md":
        text, warnings, kind = clean_text(p.read_text(encoding="utf-8")), [], "md"
    elif ext == ".pdf":
        parsed = parse_pdf(str(p))
        text, warnings, kind = parsed.text, parsed.warnings, "pdf"
    else:
        raise ValueError(f"unsupported file type: {ext}")
    chunks = split_into_chunks(text, doc_id, max_chars=max_chars)
    for i, c in enumerate(chunks):
        c["id"] = f"{doc_id}_chunk{i}"
        c["source_type"] = kind
        c["text_sha256"] = hashlib.sha256(c["text"].encode("utf-8")).hexdigest()[:16]
    if not chunks and not warnings:
        warnings = ["没有识别到任何 '## ' 标题，未产生 chunk。"]
    return chunks, warnings
