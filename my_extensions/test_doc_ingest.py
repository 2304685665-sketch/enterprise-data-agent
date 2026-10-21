"""doc_ingest（文档解析/清洗/切分）的离线测试。PDF 夹具在测试里用 pymupdf 现场生成，
不依赖外部文件；不调用任何 API。"""

import os
import sys
import tempfile

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from open_deep_research import doc_ingest as di  # noqa: E402
import eval_retrieval as ev  # noqa: E402


def _make_pdf(pages, header=None, footer_fmt=None, cjk=False):
    """pages：每页是 [(text, fontsize), ...]。header 每页顶部重复；footer_fmt 如 'Page {n}'。"""
    doc = pymupdf.open()
    font = "china-s" if cjk else "helv"
    for n, items in enumerate(pages, start=1):
        page = doc.new_page()  # 595 x 842
        if header:
            page.insert_text((72, 30), header, fontsize=9, fontname=font)
        y = 120
        for text, size in items:
            page.insert_text((72, y), text, fontsize=size, fontname=font)
            y += size + 16
        if footer_fmt:
            page.insert_text((72, 815), footer_fmt.format(n=n), fontsize=9, fontname=font)
    data = doc.tobytes()
    doc.close()
    return data


EN_PAGES = [
    [("Revenue Overview", 18), ("Revenue grew steadily across all regions this year", 11)],
    [("Cost Structure", 18), ("Costs were dominated by supplier contracts and logistics", 11)],
    [("Outlook", 18), ("The outlook remains cautious given supply chain risk", 11)],
    [("Appendix", 18), ("Additional tables are provided on request", 11)],
]


def test_english_pdf_headings_and_header_footer_removed():
    data = _make_pdf(EN_PAGES, header="ACME Confidential", footer_fmt="Page {n}")
    parsed = di.parse_pdf(data)
    assert parsed.has_text and parsed.pages == 4, parsed
    lines = parsed.text.split("\n")
    assert "## Revenue Overview" in lines and "## Appendix" in lines, lines
    assert "Revenue grew steadily across all regions this year" in lines
    assert not any("Confidential" in l for l in lines), "跨页重复的页眉必须删除"
    assert not any(l.strip().lower().startswith("page ") for l in lines), "页码页脚必须删除"
    assert not any(l.startswith("## Revenue grew") for l in lines), "正文字号不得被识别成标题"
    print("✅ test_english_pdf_headings_and_header_footer_removed 通过")


def test_short_pdf_does_not_drop_repeated_header_to_avoid_deleting_body():
    data = _make_pdf(EN_PAGES[:2], header="ACME Confidential")
    parsed = di.parse_pdf(data)
    assert "ACME Confidential" in parsed.text, "少于 3 页不做页眉删除（文档注明的保守行为）"
    print("✅ test_short_pdf_does_not_drop_repeated_header_to_avoid_deleting_body 通过")


def test_chinese_pdf_text_and_headings_preserved():
    pages = [[("融资历程", 18), ("公司已完成多轮融资，最近一轮为 Series C", 11)],
             [("技术方向", 18), ("核心技术方向是高动态平衡控制算法", 11)],
             [("团队背景", 18), ("核心团队来自机器人运动控制背景", 11)]]
    parsed = di.parse_pdf(_make_pdf(pages, header="内部资料", footer_fmt="第 {n} 页", cjk=True))
    lines = parsed.text.split("\n")
    assert "## 融资历程" in lines and "## 团队背景" in lines, lines
    assert any("Series C" in l for l in lines)
    assert not any("内部资料" in l for l in lines) and not any(l.startswith("第") and "页" in l for l in lines)
    print("✅ test_chinese_pdf_text_and_headings_preserved 通过")


def test_scanned_or_blank_pdf_reports_no_text_instead_of_silent_empty():
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    parsed = di.parse_pdf(data)
    assert parsed.has_text is False and parsed.text == "" and any("OCR" in w for w in parsed.warnings)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "scan.pdf")
        open(p, "wb").write(data)
        chunks, warnings = di.ingest_file(p)
    assert chunks == [] and warnings, "无文字层必须返回告警，不能静默入库空内容"
    print("✅ test_scanned_or_blank_pdf_reports_no_text_instead_of_silent_empty 通过")


def test_heading_heuristic_false_positive_guards():
    pages = [[("Big but ends with a period.", 18),
              ("x" * 62, 14),
              ("Normal Heading", 18),
              ("body text line one that is long enough to dominate the character count", 11),
              ("body text line two that is long enough to dominate the character count", 11),
              ("body text line three that is long enough to dominate the character count", 11)]]
    parsed = di.parse_pdf(_make_pdf(pages))
    lines = parsed.text.split("\n")
    assert "## Normal Heading" in lines
    assert "Big but ends with a period." in lines, "以句号结尾的大字号行不是标题"
    assert ("x" * 62) in lines, "超过 60 字符的大字号行不是标题"
    print("✅ test_heading_heuristic_false_positive_guards 通过")


def test_clean_text_markdown_mode_keeps_numbers_duplicates_and_cjk_punctuation():
    src = "2022\n重复行\n重复行\n公司成立，总部位于上海。\n\n\n\n结尾"
    out = di.clean_text(src)
    assert "2022" in out.split("\n"), "md 模式不得删除单独成行的数字（可能是年份）"
    assert out.split("\n").count("重复行") == 2, "md 模式不去重"
    assert "，" in out and "。" in out, "不得把全角标点规范化成半角（NFC 而非 NFKC）"
    assert "\n\n\n" not in out
    print("✅ test_clean_text_markdown_mode_keeps_numbers_duplicates_and_cjk_punctuation 通过")


def test_clean_text_pdf_mode_removes_page_numbers_and_consecutive_duplicates():
    out = di.clean_text("正文A\n12\n第 3 页\nPage 4 of 9\n正文A\n正文A\n正文B", pdf_artifacts=True)
    assert out.split("\n") == ["正文A", "正文A", "正文B"] or out.split("\n") == ["正文A", "正文B"], out
    assert "12" not in out and "第 3 页" not in out and "Page 4" not in out
    print("✅ test_clean_text_pdf_mode_removes_page_numbers_and_consecutive_duplicates 通过")


def test_split_long_sections_bounded_and_lossless():
    sentences = [f"这是第{i}句话，用来测试长段落切分。" for i in range(1, 60)]
    text = "## 长段落\n" + "".join(sentences)
    chunks = di.split_into_chunks(text, "docX", max_chars=200)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 200 + 10 for c in chunks), [len(c["text"]) for c in chunks]
    assert chunks[0]["section"] == "长段落" and chunks[1]["section"] == "长段落 (2)"
    joined = "".join(c["text"] for c in chunks).replace("\n", "")
    assert all(s in joined for s in sentences), "切分不得丢失任何句子"
    print(f"✅ test_split_long_sections_bounded_and_lossless 通过（{len(chunks)} 段）")


def test_markdown_ingest_matches_existing_index_chunking():
    mine = []
    for fn in sorted(os.listdir(ev.KB_DIR)):
        if fn.endswith(".md") and fn != "ANSWER_KEY.md":
            chunks, warnings = di.ingest_file(os.path.join(ev.KB_DIR, fn))
            assert not warnings, (fn, warnings)
            mine.extend(chunks)
    base = ev.load_kb_chunks()
    assert [(c["id"], c["doc_id"], c["section"]) for c in mine] == \
           [(c["id"], c["doc_id"], c["section"]) for c in base], "必须与现有索引的切分结果一一对应"
    norm = lambda t: "".join(t.split())
    assert [norm(c["text"]) for c in mine] == [norm(c["text"]) for c in base], "正文内容（忽略空白）必须一致"
    assert all(c["source_type"] == "md" for c in mine)
    print(f"✅ test_markdown_ingest_matches_existing_index_chunking 通过（{len(mine)} 个 chunk，id/章节/正文一致）")


def test_pdf_ingest_produces_ids_sections_and_unsupported_extension_raises():
    data = _make_pdf(EN_PAGES, header="ACME Confidential", footer_fmt="Page {n}")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "annual_report.pdf")
        open(p, "wb").write(data)
        chunks, warnings = di.ingest_file(p)
        assert not warnings
        assert [c["section"] for c in chunks] == ["Revenue Overview", "Cost Structure", "Outlook", "Appendix"]
        assert chunks[0]["id"] == "annual_report_chunk0" and chunks[0]["source_type"] == "pdf"
        bad = os.path.join(d, "x.docx")
        open(bad, "w").write("x")
        try:
            di.ingest_file(bad)
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass
    print("✅ test_pdf_ingest_produces_ids_sections_and_unsupported_extension_raises 通过")


def main():
    test_english_pdf_headings_and_header_footer_removed()
    test_short_pdf_does_not_drop_repeated_header_to_avoid_deleting_body()
    test_chinese_pdf_text_and_headings_preserved()
    test_scanned_or_blank_pdf_reports_no_text_instead_of_silent_empty()
    test_heading_heuristic_false_positive_guards()
    test_clean_text_markdown_mode_keeps_numbers_duplicates_and_cjk_punctuation()
    test_clean_text_pdf_mode_removes_page_numbers_and_consecutive_duplicates()
    test_split_long_sections_bounded_and_lossless()
    test_markdown_ingest_matches_existing_index_chunking()
    test_pdf_ingest_produces_ids_sections_and_unsupported_extension_raises()
    print("\n全部测试通过！")


if __name__ == "__main__":
    main()
