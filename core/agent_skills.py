"""Agent Skills loader（参照 agentskills.io 开放规范）。

目录形态：skills/<name>/SKILL.md（必须）+ scripts/、references/、assets/（可选）。
渐进式披露：启动时只把 name + description + SKILL.md 的绝对路径注入系统提示；
模型用 read 把完整指令读进上下文，再按文档用 bash 跑脚本
（每个技能自己的 scripts/<name>.py 自带 CLI 入口，由 bash 直接运行）。
"""
import json
import re
from pathlib import Path

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_name(name):
    errors = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name 超过 {MAX_NAME_LENGTH} 字符")
    if not NAME_RE.match(name):
        errors.append("name 只允许小写字母、数字、连字符和下划线")
    if name.endswith(("-", "_")) or "--" in name or "__" in name:
        errors.append("name 不能以下划线/连字符结尾或含连续符号")
    return errors


def parse_frontmatter(text):
    """解析 SKILL.md 顶部的 YAML frontmatter（简单 key: value 子集）。返回 (meta, body)。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    meta, body_start = {}, None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = i + 1
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")
    if body_start is None:
        return {}, text
    return meta, "\n".join(lines[body_start:]).lstrip("\n")


class AgentSkill:
    def __init__(self, name, description, directory, entry=None, parameters=None,
                 body="", errors=None):
        self.name = name
        self.description = description
        self.directory = Path(directory)
        self.entry = entry  # 相对技能目录的脚本路径，如 scripts/image_gen.py
        self.parameters = parameters or {}
        self.body = body  # SKILL.md 正文（渐进式披露的完整指令）
        self.errors = errors or []

    @property
    def has_entry(self):
        return bool(self.entry)

    @property
    def instructions_only(self):
        return not self.entry

    def scripts(self):
        scripts_dir = self.directory / "scripts"
        if not scripts_dir.is_dir():
            return []
        return sorted(p.name for p in scripts_dir.iterdir() if p.is_file())

    def entry_path(self):
        return self.directory / self.entry if self.entry else None


def load_skill(skill_dir):
    """加载单个技能目录，返回 AgentSkill（无效时 errors 非空）。"""
    skill_dir = Path(skill_dir)
    md_path = skill_dir / "SKILL.md"
    errors = []
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return AgentSkill(skill_dir.name, "", skill_dir, errors=[f"读取 SKILL.md 失败: {exc}"])
    meta, body = parse_frontmatter(text)
    name = (meta.get("name") or skill_dir.name).strip()
    description = (meta.get("description") or "").strip()
    errors.extend(_validate_name(name))
    if not description:
        errors.append("缺少 description（决定模型何时加载此技能）")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"description 超过 {MAX_DESCRIPTION_LENGTH} 字符")

    entry = (meta.get("entry") or "").strip()
    if entry:
        target = skill_dir / entry
        if not target.is_file():
            errors.append(f"entry 脚本不存在: {entry}")
            entry = None

    parameters = {}
    params_path = skill_dir / "parameters.json"
    if params_path.is_file():
        try:
            parameters = json.loads(params_path.read_text(encoding="utf-8"))
            if not isinstance(parameters, dict):
                raise ValueError("parameters.json 必须是 JSON 对象")
        except (OSError, ValueError) as exc:
            errors.append(f"parameters.json 无效: {exc}")
            parameters = {}
    return AgentSkill(name, description, skill_dir, entry=entry,
                      parameters=parameters, body=body, errors=errors)


def scan(skills_dir, skip_names=()):
    """扫描技能目录下所有含 SKILL.md 的一级子目录。

    返回 (skills: {name: AgentSkill}, diagnostics: [(name, [errors])])。
    skip_names 中的技能（如与内置技能重名）仍会加载，但调用方自行决定优先级。
    """
    root = Path(skills_dir)
    skills, diagnostics = {}, []
    if not root.is_dir():
        return skills, diagnostics
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if not (child / "SKILL.md").is_file():
            continue
        skill = load_skill(child)
        if skill.errors and not skill.description:
            diagnostics.append((child.name, skill.errors))
            continue
        if skill.name in skills:
            diagnostics.append((skill.name, ["技能名重复，保留先发现的"]))
            continue
        if skill.errors:
            diagnostics.append((skill.name, skill.errors))
        skills[skill.name] = skill
    return skills, diagnostics
