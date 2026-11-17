"""技能权限识别与依赖的实时分析。

目录技能是纯 Markdown 指令：模型读完 SKILL.md 后用 ``bash`` 跑
``skills/<dir>/scripts/<file>.py``。执行期看不到 ``skill_name``，只能从
命令里反查技能名。本模块只提供纯函数，判定入口仍在
``harness._execute_tool`` 的 ``tool_access`` 一层。

依赖不维护手写表：授权时现场读 SKILL.md、脚本源码和 harness 的工具实现，
分析该能力会连带用到哪些权限，再决定是直接落库还是请主人确认。
"""
import ast
import re
import threading
import time
from pathlib import Path

from core import agent_skills

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
HARNESS_SRC = Path(__file__).with_name("harness.py")

# skills/<dir>/scripts/<file>.py。前导负向断言挡住 ./ 与绝对路径。
_SCRIPT = re.compile(
    r"(?<![\w./\\-])skills[/\\]([A-Za-z0-9_-]+)[/\\]scripts[/\\]([A-Za-z0-9_.-]+\.py)(?![\w/-])",
    re.IGNORECASE,
)
_PY_TOKEN = re.compile(r"py(?:thon)?3?(?:\.exe)?", re.IGNORECASE)
_SCOPE_ARG = re.compile(r"--scope\s+[A-Za-z0-9_:.-]+")
_SHELL_META = re.compile(r"[;|`$\n\r<>]|&&|\|\||\$\(|&")

_catalog_lock = threading.Lock()
_catalog_cache = {"expires": 0.0, "value": {}}
_dep_lock = threading.Lock()
_dep_cache = {}


def skill_catalog():
    """技能名 -> 技能目录。技能名取 SKILL.md frontmatter，和目录名不一定相同。"""
    now = time.time()
    with _catalog_lock:
        if _catalog_cache["expires"] > now:
            return dict(_catalog_cache["value"])
        skills, _ = agent_skills.scan(SKILLS_DIR)
        value = {name: info.directory for name, info in skills.items()}
        _catalog_cache["value"] = value
        _catalog_cache["expires"] = now + 30
        return dict(value)


def normalize_skill(name):
    """主人可能说目录名（image-gen）或技能名（image_gen），统一成技能名。"""
    value = str(name or "").strip()
    if not value or value == "*":
        return ""
    catalog = skill_catalog()
    if value in catalog:
        return value
    for skill_name, directory in catalog.items():
        if directory.name == value:
            return skill_name
    return ""


def _meta_outside_quotes(text):
    """引号外的 shell 元字符。参数 JSON 里的 & ; 属于数据，不该误伤。"""
    out, quote = [], ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            out.append(" ")
        elif char in ("'", '"'):
            quote = char
            out.append(" ")
        else:
            out.append(char)
    return _SHELL_META.search("".join(out))


def skill_from_command(command):
    """把一条 bash 命令识别成纯技能脚本调用，返回技能名；否则返回空串。

    只有整条命令是「python + 一个真实存在的技能脚本」才认。夹带第二条命令、
    第二个脚本、绝对路径或不存在的脚本一律回落到普通 bash 工具判定。
    """
    if not isinstance(command, str) or not command.strip() or len(command) > 4000:
        return ""
    text = command.strip()
    if _meta_outside_quotes(text):
        return ""
    # --arguments 后面是数据（可能含 JSON），只分析它之前的执行部分。
    head = text.split("--arguments", 1)[0].strip()
    match = _SCRIPT.search(head)
    if not match or _SCRIPT.search(head, match.end()):
        return ""
    prefix = head[:match.start()].split()
    if not prefix or not _PY_TOKEN.fullmatch(prefix[0]):
        return ""
    if any(not part.startswith("-") for part in prefix[1:]):
        return ""
    suffix = head[match.end():].strip()
    if suffix and not _SCOPE_ARG.fullmatch(suffix):
        return ""
    dir_name, script = match.group(1), match.group(2)
    if ".." in dir_name or ".." in script:
        return ""
    skill_name = ""
    for name, directory in skill_catalog().items():
        if directory.name == dir_name:
            skill_name = name
            break
    if not skill_name:
        return ""
    if not (skill_catalog()[skill_name] / "scripts" / script).is_file():
        return ""
    return skill_name


def is_skill_doc(path):
    """是否技能目录内的说明文档。读 SKILL.md 是基础能力，不需要单独授权，
    但不能因此放开整个 read 工具（那等于开放任意本机文件）。"""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or ".." in raw or not raw.lower().endswith(".md"):
        return False
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            # bash 的 cwd 不一定是项目根，相对路径按项目根解析。
            candidate = SKILLS_DIR.parent / candidate
        return candidate.resolve().is_relative_to(SKILLS_DIR.resolve())
    except (OSError, ValueError, TypeError):
        return False


def _tool_names():
    from core.harness import HARNESS_TOOLS

    return {item["function"]["name"] for item in HARNESS_TOOLS}


def _base_tools():
    from core.qq_authority import BASE_TOOLS

    return set(BASE_TOOLS)


def _sources(skill_name):
    """返回 [(来源名, 文本, 是否脚本)]。文档去掉 frontmatter，避免把描述里的
    mode=edit 之类参数值误判成工具依赖。"""
    directory = skill_catalog().get(skill_name)
    if not directory:
        return []
    out = []
    md = directory / "SKILL.md"
    if md.is_file():
        try:
            _, body = agent_skills.parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            out.append((md.name, body, False))
        except OSError:
            pass
    scripts = directory / "scripts"
    if scripts.is_dir():
        for path in sorted(scripts.glob("*.py")):
            try:
                out.append((path.name, path.read_text(encoding="utf-8", errors="replace"), True))
            except OSError:
                continue
    return out


def _skill_dependencies(skill_name):
    """实时扫 SKILL.md 正文与脚本源码，找出该技能会连带用到的其它权限。

    文档只认反引号标注的工具调用（如「先用 `read` 读完」）；脚本源码不做
    工具名文本匹配（变量名噪声太大），只识别跨技能脚本引用。脚本内部起子进程
    不经过 harness 工具链，因此不算工具依赖。
    """
    found = {}
    catalog = skill_catalog()
    # read 读技能说明由 is_skill_doc 特例放行，不构成需要主人确认的依赖。
    tools = _tool_names() - _base_tools() - {"bash", "read", skill_name}
    for source, text, is_script in _sources(skill_name):
        if not is_script:
            for tool in sorted(tools):
                if re.search(r"`" + re.escape(tool) + r"`", text, re.I):
                    found.setdefault(
                        ("tool", tool),
                        f"{source} 要求模型调用工具 {tool}，该工具默认未授权",
                    )
        for dir_name, _ in _SCRIPT.findall(text):
            for other, directory in catalog.items():
                if directory.name == dir_name and other != skill_name:
                    found.setdefault(
                        ("skill", other),
                        f"{source} 中调用了技能 {other} 的脚本",
                    )
    return [
        {"kind": kind, "name": name, "reason": reason}
        for (kind, name), reason in sorted(found.items())
    ]


def _is_name_eq(test, variable, value):
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == variable
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == value
    )


def _tool_dependencies(tool_name):
    """实时解析 harness 的工具实现，找出它会嵌套调用的其它工具。"""
    try:
        stamp = HARNESS_SRC.stat().st_mtime_ns
    except OSError:
        return []
    with _dep_lock:
        cached = _dep_cache.get(tool_name)
        if cached and cached[0] == stamp:
            return [dict(row) for row in cached[1]]
    try:
        tree = ast.parse(HARNESS_SRC.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    base = _base_tools()
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_execute_tool":
            continue
        for branch in ast.walk(node):
            if not isinstance(branch, ast.If) or not _is_name_eq(branch.test, "name", tool_name):
                continue
            for call in ast.walk(branch):
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                func = call.func
                if not isinstance(func, ast.Attribute) or func.attr != "_dispatch_tool":
                    continue
                arg = call.args[0]
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                dep = arg.value
                if dep == tool_name or dep in base:
                    continue
                found[dep] = f"harness 中 {tool_name} 的实现会调用工具 {dep}"
    rows = [
        {"kind": "tool", "name": name, "reason": reason}
        for name, reason in sorted(found.items())
    ]
    with _dep_lock:
        _dep_cache[tool_name] = (stamp, rows)
    return [dict(row) for row in rows]


def analyze_dependencies(kind, name):
    """返回一个能力依赖的其它权限；空列表表示可以直接授权。"""
    if kind == "skill":
        return _skill_dependencies(name)
    if kind == "tool":
        return _tool_dependencies(name)
    return []
