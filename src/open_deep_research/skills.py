"""Skill 模块：把"流程性知识"从提示词里拆出来，做成可加载、可校验、按需注入的文件。

每个 skill 是 skills/<name>/SKILL.md：
    ---
    name: sql-analysis
    description: 一句话说明用途
    triggers: 关键词1, 关键词2, ...
    ---
    正文（步骤/规范）

只实现三件事：加载与校验（坏文件跳过并给出原因，不抛异常）、按问题文本的触发词挑选、
渲染成可拼进 system prompt 的片段。选择是确定性的关键词匹配——不是语义匹配，
没有触发词命中的问题得不到任何 skill（这是刻意的保守取舍，也是局限）。
默认关闭：环境变量 DEEP_RESEARCH_SKILLS=1 才会在 researcher 的提示词里注入。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional, Sequence

SKILLS_ENV_VAR = "DEEP_RESEARCH_SKILLS"
DEFAULT_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "skills")
MAX_SKILL_BODY_CHARS = 2000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str


def skills_enabled() -> bool:
    return os.environ.get(SKILLS_ENV_VAR, "").strip() == "1"


def parse_skill_md(text: str) -> Skill:
    """解析一个 SKILL.md；格式不合法抛 ValueError（load_skills 会捕获）。"""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", text, flags=re.S)
    if not m:
        raise ValueError("缺少 --- 包裹的 frontmatter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"frontmatter 行无法解析: {line!r}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    for key in ("name", "description", "triggers"):
        if not meta.get(key):
            raise ValueError(f"frontmatter 缺少 {key}")
    if not _NAME_RE.match(meta["name"]):
        raise ValueError(f"name 不合法: {meta['name']!r}（小写字母/数字/连字符）")
    triggers = tuple(t.strip().lower() for t in re.split(r"[,，]", meta["triggers"]) if t.strip())
    if not triggers:
        raise ValueError("triggers 为空")
    body = m.group(2).strip()
    if not body:
        raise ValueError("正文为空")
    if len(body) > MAX_SKILL_BODY_CHARS:
        raise ValueError(f"正文超过 {MAX_SKILL_BODY_CHARS} 字符")
    return Skill(meta["name"], meta["description"], triggers, body)


def load_skills(skills_dir: Optional[str] = None) -> tuple[list[Skill], list[str]]:
    """加载目录下所有 <name>/SKILL.md。返回 (skills, errors)；目录不存在返回空。
    目录名必须与 frontmatter 的 name 一致；重名只保留第一个。按目录名排序，保证确定性。"""
    root = skills_dir or DEFAULT_SKILLS_DIR
    skills: list[Skill] = []
    errors: list[str] = []
    if not os.path.isdir(root):
        return skills, errors
    seen: set[str] = set()
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                sk = parse_skill_md(f.read())
            if sk.name != entry:
                raise ValueError(f"目录名 {entry!r} 与 name {sk.name!r} 不一致")
            if sk.name in seen:
                raise ValueError(f"重名 skill: {sk.name}")
        except (ValueError, OSError, UnicodeDecodeError) as e:
            errors.append(f"{entry}: {e}")
            continue
        seen.add(sk.name)
        skills.append(sk)
    return skills, errors


def select_skills(text: str, skills: Sequence[Skill], max_skills: int = 2) -> list[Skill]:
    """按触发词命中数降序挑选（同数按加载顺序），最多 max_skills 个；0 命中的不选。"""
    low = (text or "").lower()
    scored = []
    for i, sk in enumerate(skills):
        hits = sum(1 for t in sk.triggers if t in low)
        if hits:
            scored.append((-hits, i, sk))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [sk for _, _, sk in scored[:max_skills]]


def render_skills_prompt(selected: Sequence[Skill]) -> str:
    if not selected:
        return ""
    parts = ["<Skills>", "以下是与当前问题相关的流程规范，请遵循（它们不改变工具本身的能力与限制）："]
    for sk in selected:
        parts.append(f"## {sk.name} — {sk.description}\n{sk.body}")
    parts.append("</Skills>")
    return "\n".join(parts)


def skills_prompt_for(text: str, skills_dir: Optional[str] = None) -> str:
    """researcher 调用的入口：未启用返回空串；加载失败的 skill 被跳过，不影响研究流程。"""
    if not skills_enabled():
        return ""
    skills, _errors = load_skills(skills_dir)
    return render_skills_prompt(select_skills(text, skills))
