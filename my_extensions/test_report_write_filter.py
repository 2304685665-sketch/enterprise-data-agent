import importlib.util, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", "src", "open_deep_research", name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
F = _load("report_write_filter")

def test_removes_fenced_inline_and_bare_lines():
    r = "结论：未修改。\n```sql\nDELETE FROM funding WHERE year < 2025;\n```\n可执行 `UPDATE funding SET amount=1` 来改。\nINSERT INTO funding VALUES (1);\n完。"
    out, n = F.strip_write_sql(r)
    assert n == 3 and not F.WRITE_RE.search(out) and "结论：未修改。" in out and out.endswith("完。")

def test_read_only_report_unchanged():
    r = "共 12 笔。\n```sql\nSELECT COUNT(*) FROM funding;\n```\n用 `SELECT sum(amount)` 得到。\n日期更新于 2026 年。"
    assert F.strip_write_sql(r) == (r, 0)

def test_gated_by_flag_and_intent(monkeypatch):
    r = "```sql\nDROP TABLE funding;\n```"
    monkeypatch.delenv("ODR_WRITE_SQL_FILTER", raising=False)
    assert F.filter_report_for_write_request(r, True) == r          # 默认关闭
    monkeypatch.setenv("ODR_WRITE_SQL_FILTER", "1")
    assert F.filter_report_for_write_request(r, False) == r         # 非写请求不动
    assert "DROP" not in F.filter_report_for_write_request(r, True)

def test_known_cost_read_select_in_same_block_is_lost():
    r = "```sql\nSELECT * FROM funding;\nDELETE FROM funding;\n```"
    out, n = F.strip_write_sql(r)
    assert n == 1 and "SELECT" not in out   # 已知代价，写在模块文档里
