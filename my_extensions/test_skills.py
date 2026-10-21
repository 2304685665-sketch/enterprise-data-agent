"""skills 模块（加载/校验/挑选/渲染/默认关闭）及其接入 researcher 的离线测试，无任何模型调用。"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")

from open_deep_research import skills as sk  # noqa: E402

GOOD = "---\nname: demo-skill\ndescription: 演示\ntriggers: 融资, funding\n---\n步骤一\n步骤二\n"


def _mk(root, name, text):
    d = os.path.join(root, name)
    os.makedirs(d)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(text)


def test_parse_good_skill():
    s = sk.parse_skill_md(GOOD)
    assert (s.name, s.description, s.triggers) == ("demo-skill", "演示", ("融资", "funding"))
    assert s.body == "步骤一\n步骤二"
    print("✅ test_parse_good_skill 通过")


def test_parse_rejects_bad_files():
    bad = {
        "no frontmatter": "just text",
        "missing triggers": "---\nname: a\ndescription: d\n---\nbody",
        "bad name": "---\nname: Bad_Name\ndescription: d\ntriggers: x\n---\nbody",
        "empty body": "---\nname: a\ndescription: d\ntriggers: x\n---\n\n",
        "too long": "---\nname: a\ndescription: d\ntriggers: x\n---\n" + "字" * (sk.MAX_SKILL_BODY_CHARS + 1),
        "bad line": "---\nname: a\nnonsense\ndescription: d\ntriggers: x\n---\nbody",
    }
    for label, text in bad.items():
        try:
            sk.parse_skill_md(text)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝: {label}")
    print("✅ test_parse_rejects_bad_files 通过")


def test_load_skips_bad_reports_errors_and_is_sorted():
    with tempfile.TemporaryDirectory() as root:
        _mk(root, "zeta", GOOD.replace("demo-skill", "zeta"))
        _mk(root, "alpha", GOOD.replace("demo-skill", "alpha"))
        _mk(root, "broken", "no frontmatter")
        _mk(root, "mismatch", GOOD)  # name=demo-skill 与目录名不一致
        skills, errors = sk.load_skills(root)
        assert [s.name for s in skills] == ["alpha", "zeta"], skills
        assert len(errors) == 2 and any("broken" in e for e in errors) and any("不一致" in e for e in errors), errors
    assert sk.load_skills("/nonexistent/dir") == ([], [])
    print("✅ test_load_skips_bad_reports_errors_and_is_sorted 通过")


def test_real_repo_skills_all_valid():
    skills, errors = sk.load_skills()
    assert errors == [], errors
    assert {s.name for s in skills} >= {"sql-analysis", "internal-doc-citation", "source-routing"}
    print(f"✅ test_real_repo_skills_all_valid 通过（{len(skills)} 个）")


def test_select_by_trigger_count_limit_and_no_match():
    a = sk.Skill("a", "d", ("融资", "funding"), "A")
    b = sk.Skill("b", "d", ("融资",), "B")
    c = sk.Skill("c", "d", ("天气",), "C")
    assert [s.name for s in sk.select_skills("Funding 融资 情况", [b, a, c])] == ["a", "b"]  # a 命中 2 次
    assert [s.name for s in sk.select_skills("融资", [b, a, c], max_skills=1)] == ["b"]  # 同数按加载顺序
    assert sk.select_skills("毫不相关", [a, b, c]) == [] and sk.select_skills("", [a]) == []
    print("✅ test_select_by_trigger_count_limit_and_no_match 通过")


def test_render_and_default_off():
    a = sk.Skill("a", "说明", ("x",), "正文A")
    out = sk.render_skills_prompt([a])
    assert out.startswith("<Skills>") and "## a — 说明\n正文A" in out and out.endswith("</Skills>")
    assert sk.render_skills_prompt([]) == ""
    os.environ.pop(sk.SKILLS_ENV_VAR, None)
    assert sk.skills_prompt_for("融资 数据库") == ""
    os.environ[sk.SKILLS_ENV_VAR] = "1"
    try:
        assert "sql-analysis" in sk.skills_prompt_for("请查询数据库里的融资记录")
        assert sk.skills_prompt_for("今天天气") == ""
    finally:
        os.environ.pop(sk.SKILLS_ENV_VAR, None)
    print("✅ test_render_and_default_off 通过")


# ---------- 接入 researcher 节点（假模型） ----------

def _run_researcher(topic, env_on):
    from langchain_core.messages import HumanMessage
    from open_deep_research import deep_researcher as dr

    captured = {}

    class FakeBound:
        async def ainvoke(self, messages):
            captured["system"] = messages[0].content
            from langchain_core.messages import AIMessage
            return AIMessage(content="ok")

    class FakeModel:
        def bind_tools(self, tools):
            return self

        def with_retry(self, **kw):
            return self

        def with_config(self, cfg):
            return FakeBound()

    async def fake_tools(config):
        return [object()]

    orig = (dr.configurable_model, dr.get_all_tools)
    dr.configurable_model, dr.get_all_tools = FakeModel(), fake_tools
    if env_on:
        os.environ[sk.SKILLS_ENV_VAR] = "1"
    else:
        os.environ.pop(sk.SKILLS_ENV_VAR, None)
    try:
        asyncio.run(dr.researcher({"researcher_messages": [HumanMessage(content=topic)]}, {"configurable": {}}))
    finally:
        dr.configurable_model, dr.get_all_tools = orig
        os.environ.pop(sk.SKILLS_ENV_VAR, None)
    return captured["system"]


def test_researcher_prompt_injects_only_when_enabled_and_matched():
    off = _run_researcher("查询数据库里的融资记录", env_on=False)
    on = _run_researcher("查询数据库里的融资记录", env_on=True)
    miss = _run_researcher("今天天气如何", env_on=True)
    assert "<Skills>" not in off and "<Skills>" in on and "sql-analysis" in on
    assert miss == off, "没有触发词命中时提示词必须与关闭时完全一致"
    assert on.startswith(off), "注入只能是追加，不改动原提示词"
    print("✅ test_researcher_prompt_injects_only_when_enabled_and_matched 通过")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
