"""Pi harness 的结构化 system prompt。

结构与装配规则移植自 ``pi/packages/coding-agent/src/core/system-prompt.ts``（以及与它
直接相关的 ``tools/*.ts`` 的 ``*ToolSystemPromptContribution``、``skills.ts`` 的
``formatSkillsForPrompt``、``config.ts`` 的 ``get*Path``、``pi-ai/utils/text.ts`` 的
``getSystemMessageText``）。

这里只放 pi 的 section 文本与装配规则，不掺 miniBot 的业务文字：miniBot 的人设、
中文行为准则、技能使用说明由 ``core/context_builder.py`` 以 **独立 section** 叠加。

MiniBot 本地化偏离（同步 pi 时逐条复核）
--------------------------------------
下列文字与 pi 原文不同，其余 section 文本、顺序与装配规则仍保持逐行可对照。
两条主线：① 宿主是 MiniBot 不是 pi；② pi 的 ``bash`` 真的跑 bash，MiniBot 的
``bash`` 在 Windows 上由 ``powershell.exe -Command`` 执行（见 ``core/harness.py``），
也没有 ``PI_*`` 环境变量和独立的 grep/find/ls 工具 —— 照抄会让模型写 bash-only
语法、自己给内联 JSON 换引号或改用 ``Get-Content`` 展开，导致技能脚本的
``--arguments`` 被 PowerShell 吞掉双引号（``_protect_inline_skill_arguments``）。

* ``PI_PREAMBLE``：宿主名 pi → MiniBot
* ``PI_TOOL_SNIPPETS["bash"]``：声明真实 shell
* ``PI_TOOL_GUIDELINES["bash"]``：PowerShell 语法 + 内联 JSON 原样传递
* ``build_rules()`` 的文件操作条件规则：改用 shell 措辞
* ``format_skills_for_prompt()`` 的 bash hint：改用 shell 措辞

section 顺序（``build_system_prompt_sections`` 的插入顺序）与 pi 一致：

    preamble(裸文本) / tools / rules / docs / addendum / project_context /
    skills / cwd / <custom sections>

pi 的 section 值自带 ``<name>\\n...\\n</name>`` 包裹；``preamble`` 不带标签。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# system-prompt.ts: promptSections.preamble 的默认分支
# pi 原文是 "...running inside pi, a coding agent harness."；只换宿主名，其余逐字不动。
# 人设由 <persona> section 独立给出，不写进这里。
PI_PREAMBLE = (
    "You are a coding assistant running inside MiniBot, an agent harness. "
    "Help users inspect files, run commands, and modify code."
)

# packages/coding-agent/src/core/tools/*.ts 的 *ToolSystemPromptContribution
# pi 原文 "Execute bash commands (ls, grep, find, etc.)"：MiniBot 在 Windows 上
# 实际跑 powershell.exe，bash-only 的 ls/rg/find 会拼出模型以为正确的错命令。
PI_TOOL_SNIPPETS: Dict[str, str] = {
    "read": "Read file contents",
    "bash": "Execute terminal commands (PowerShell on Windows, shell on Linux/macOS)",
    "edit": "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "write": "Create or overwrite files",
    "grep": "Search file contents for patterns (respects .gitignore)",
    "find": "Find files by glob pattern (respects .gitignore)",
    "ls": "List directory contents",
    "powershell": "Execute PowerShell commands",
}

PI_TOOL_GUIDELINES: Dict[str, Sequence[str]] = {
    "read": ("Use read to examine files instead of cat or sed.",),
    # pi 原文是 "You can inspect PI_* environment variables..."，MiniBot 没有这些
    # 变量。换成执行环境说明 + 内联 JSON 的传递约定，直接堵住日志里那种
    # write 参数文件再用 Get-Content 展开、把双引号丢光的绕路。
    "bash": (
        "On Windows the shell is PowerShell: use PowerShell syntax, not bash-only commands.",
        "Pass a skill's --arguments JSON inline exactly as written in SKILL.md, wrapped in single quotes; "
        "do not pre-escape the quotes, split it into a file, or expand it with Get-Content.",
    ),
    "powershell": ("You can inspect PI_* environment variables for current model and session details.",),
    "edit": (
        "Use edit for targeted changes. Batch changes to one file in a single call.",
        "Each edits[].oldText must exactly and uniquely match the original file. "
        "Keep matches short and non-overlapping; merge nearby changes without including large unchanged regions.",
    ),
    "write": ("Use write only for new files or complete rewrites.",),
    "grep": (),
    "find": (),
    "ls": (),
}

# system-prompt.ts: normalizeBuildSystemPromptOptions 的默认值
DEFAULT_SELECTED_TOOLS: Sequence[str] = ("read", "bash", "edit", "write")
SYSTEM_PROMPT_SECTION_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# config.ts 的 get*Path
# ---------------------------------------------------------------------------


def get_package_dir() -> str:
    """``<docs>`` 的文档根：MiniBot 项目自身，不再指向 pi 仓库。

    ``MINIBOT_DOCS_ROOT`` 优先（兼容旧的 ``PI_PACKAGE_DIR``），否则取本文件所在
    core/ 的上一级，即项目根目录。路径全部指向真实存在的 MiniBot 文档。
    """
    env_dir = os.environ.get("MINIBOT_DOCS_ROOT") or os.environ.get("PI_PACKAGE_DIR")
    if env_dir:
        return str(Path(env_dir).resolve())
    return str(Path(__file__).resolve().parents[1].resolve())


def get_readme_path() -> str:
    # 与 pi 的 cwd section 一致，用正斜杠；反斜杠在提示词/JSON 里容易被当转义。
    return (Path(get_package_dir()) / "README.md").resolve().as_posix()


def get_docs_path() -> str:
    return (Path(get_package_dir()) / "docs").resolve().as_posix()


def get_examples_path() -> str:
    return (Path(get_package_dir()) / "examples").resolve().as_posix()


def build_docs_section() -> str:
    """``system-prompt.ts: promptSections.docs`` 的行结构 + MiniBot 真实文档。

    路径与主题清单换成 MiniBot 自己的 README / docs / examples（pi 原文里的
    extensions / themes / TUI / SDK 在 MiniBot 不存在，照搬只会把模型引到空路径）。
    行结构、触发条件与末句沿用 pi。
    """
    return (
        "MiniBot documentation (read only when the user asks about the harness itself, "
        "its tools, skills, channels, or configuration):\n"
        f"- Main documentation: {get_readme_path()}\n"
        f"- Additional docs: {get_docs_path()}\n"
        f"- Examples: {get_examples_path()} (custom skills, tool usage, test cases)\n"
        "- When reading MiniBot docs or examples, resolve docs/... under Additional docs "
        "and examples/... under Examples, not the current working directory\n"
        "- When asked about: the agent loop (docs/harness-verification.md), verification "
        "(docs/verification.md), context compaction (docs/context-compaction.md), response "
        "chain performance (docs/response-chain-performance.md), custom skills "
        "(skills/<name>/SKILL.md, skills/skill-creator/SKILL.md), configuration "
        "(config.example.json), channels (channels/), WebUI (webui/), tests (tests/)\n"
        "- Before implementing harness changes, read the relevant docs and examples "
        "in full, following related cross-references."
    )


# ---------------------------------------------------------------------------
# system-prompt.ts 的渲染函数
# ---------------------------------------------------------------------------


def render_project_context(context_files: Iterable[Mapping[str, str]]) -> str:
    return "\n\n".join(
        [
            "Project-specific instructions and guidelines:",
            *[
                f'<project_instructions path="{item.get("path", "")}">\n'
                f'{item.get("content", "")}\n'
                "</project_instructions>"
                for item in context_files
            ],
        ]
    )


def build_rules(
    selected_tools: Sequence[str],
    tool_guidelines: Mapping[str, Sequence[str]],
    prompt_guidelines: Iterable[str],
) -> str:
    """``buildRules``：条件规则 → 工具准则 → 扩展准则 → 两条固定收尾准则。"""
    rules: List[str] = []
    seen = set()

    def add_rule(rule: str) -> None:
        normalized = rule.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        rules.append(normalized)

    has_bash = "bash" in selected_tools
    has_powershell = "powershell" in selected_tools
    has_grep = "grep" in selected_tools
    has_find = "find" in selected_tools
    has_ls = "ls" in selected_tools

    if (has_bash or has_powershell) and not has_grep and not has_find and not has_ls:
        # pi 原文在 bash-only 分支写 "Use bash for file operations like ls, rg, find"。
        # MiniBot 只声明 bash 且其背后是 PowerShell，所以一律用 shell 措辞。
        if has_bash and has_powershell:
            add_rule("Use the shell tool for file operations like listing, searching, and finding files")
        elif has_powershell:
            add_rule("Use PowerShell for file operations like listing, searching, and finding files")
        else:
            add_rule("Use the shell tool for file operations like listing, searching, and finding files")

    for name in selected_tools:
        for rule in tool_guidelines.get(name, ()):
            add_rule(rule)
    for rule in prompt_guidelines:
        add_rule(rule)
    add_rule("Be concise in your responses")
    add_rule("Show file paths clearly when working with files")
    return "\n".join(f"- {rule}" for rule in rules)


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_skills_for_prompt(
    skills: Iterable[Mapping[str, Any]],
    file_read_tool: str = "read",
    tool_name: Optional[str] = None,
) -> str:
    """``formatSkillsForPrompt``：pi 的技能目录原文。

    ``tool_name`` 供非 pi 宿主覆盖提示里出现的读文件工具名。miniBot 的模型侧
    工具名已统一成 pi 的 ``read``，所以默认 None 即用 pi 原文逐字一致。
    """
    visible = [s for s in skills if not s.get("disableModelInvocation")]
    if not visible:
        return ""
    hint = {
        "read": "Use the read tool to load a skill's file when the task matches its description.",
        # pi 原文 "Use bash to load ..."；工具名虽叫 bash，实际 shell 是 PowerShell。
        "bash": "Use the shell tool to load a skill's file when the task matches its description.",
    }.get(file_read_tool, "Load a skill's file when the task matches its description.")
    if tool_name and tool_name != file_read_tool:
        hint = hint.replace(f"the {file_read_tool} tool", f"the {tool_name} tool")
    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        hint,
        "Resolve relative paths against the skill's directory (parent of SKILL.md). "
        "Use absolute paths in tool calls.",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(str(skill.get('name', '')))}</name>")
        lines.append(
            f"    <description>{_escape_xml(str(skill.get('description', '')))}</description>"
        )
        lines.append(
            f"    <location>{_escape_xml(str(skill.get('filePath') or skill.get('location', '')))}</location>"
        )
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def normalize_build_system_prompt_options(
    options: Mapping[str, Any],
) -> Dict[str, Any]:
    """``normalizeBuildSystemPromptOptions``：把所有可缺省字段补齐。"""
    return {
        "customPrompt": options.get("customPrompt"),
        "forceSystemPrompt": options.get("forceSystemPrompt"),
        "selectedTools": list(options.get("selectedTools") or DEFAULT_SELECTED_TOOLS),
        "hiddenTools": list(options.get("hiddenTools") or ()),
        "toolSnippets": dict(options.get("toolSnippets") or {}),
        "toolGuidelines": {
            name: list(guidelines)
            for name, guidelines in (options.get("toolGuidelines") or {}).items()
        },
        "promptGuidelines": list(options.get("promptGuidelines") or ()),
        "appendSystemPrompt": options.get("appendSystemPrompt") or "",
        "sections": dict(options.get("sections") or {}),
        "cwd": options.get("cwd") or "",
        "contextFiles": [dict(item) for item in (options.get("contextFiles") or ())],
        "skills": [dict(item) for item in (options.get("skills") or ())],
    }


def build_system_prompt_sections(
    options: Mapping[str, Any], skill_read_tool_name: Optional[str] = None
) -> Dict[str, str]:
    """``buildSystemPromptSections``：按 pi 的顺序返回已包裹好标签的 sections。

    ``skill_read_tool_name`` 只影响 <skills> 提示里的工具名，默认与 pi 一致。
    """
    normalized = normalize_build_system_prompt_options(options)
    custom_prompt = normalized["customPrompt"]
    selected_tools = normalized["selectedTools"]
    hidden_tools = normalized["hiddenTools"]
    tool_snippets = normalized["toolSnippets"]
    tool_guidelines = normalized["toolGuidelines"]
    prompt_guidelines = normalized["promptGuidelines"]
    append_system_prompt = normalized["appendSystemPrompt"]
    custom_sections = normalized["sections"]
    cwd = normalized["cwd"]
    context_files = normalized["contextFiles"]
    skills = normalized["skills"]

    for name in custom_sections:
        if not SYSTEM_PROMPT_SECTION_NAME.match(name) or name == "preamble":
            raise ValueError(f"Invalid system prompt section name: {name}")

    declared_tools = [name for name in selected_tools if name not in hidden_tools]
    prompt_sections: Dict[str, str] = {}
    if custom_prompt:
        prompt_sections["preamble"] = custom_prompt
    else:
        prompt_sections["preamble"] = PI_PREAMBLE
        visible_tools = [name for name in declared_tools if tool_snippets.get(name)]
        tools = (
            "\n".join(f"- {name}: {tool_snippets[name]}" for name in visible_tools)
            if visible_tools
            else "(none)"
        )
        prompt_sections["tools"] = f"{tools}\n\nAdditional tools may be available."
        prompt_sections["rules"] = build_rules(
            declared_tools, tool_guidelines, prompt_guidelines
        )
        prompt_sections["docs"] = build_docs_section()

    if append_system_prompt:
        prompt_sections["addendum"] = append_system_prompt
    if context_files:
        prompt_sections["project_context"] = render_project_context(context_files)

    # A hidden reader is still reachable through another tool, so skills stay
    # but the hint names no tool.
    readers = ("read", "bash")
    skill_file_read_tool = next(
        (tool for tool in readers if tool in declared_tools), None
    )
    if skill_file_read_tool is None and any(tool in selected_tools for tool in readers):
        skill_file_read_tool = "indirect"
    if skill_file_read_tool and skills:
        skills_prompt = format_skills_for_prompt(
            skills, skill_file_read_tool, skill_read_tool_name
        ).strip()
        if skills_prompt:
            prompt_sections["skills"] = skills_prompt
    prompt_sections["cwd"] = cwd.replace("\\", "/")
    for name, content in custom_sections.items():
        if content:
            prompt_sections[name] = content

    sections: Dict[str, str] = {"preamble": prompt_sections["preamble"]}
    for name, content in prompt_sections.items():
        if name != "preamble":
            sections[name] = f"<{name}>\n{content}\n</{name}>"
    return sections


def build_system_prompt_state(options: Mapping[str, Any]) -> Dict[str, Any]:
    """``buildSystemPromptState``：forced prompt 不分段，其余用结构化 sections。"""
    forced = options.get("forceSystemPrompt")
    if forced is not None:
        return {"content": forced, "sections": None}
    return {"content": "", "sections": build_system_prompt_sections(options)}


def build_system_prompt(options: Mapping[str, Any]) -> str:
    """``buildSystemPrompt`` / ``getSystemMessageText``：content + sections，``\\n\\n`` 连接。"""
    state = build_system_prompt_state(options)
    parts = [state["content"]]
    parts.extend(text for text in (state["sections"] or {}).values() if text is not None)
    return "\n\n".join(part for part in parts if part)


def diff_system_prompt_sections(
    previous: Mapping[str, Optional[str]],
    current: Mapping[str, str],
) -> Optional[Dict[str, Optional[str]]]:
    """``diffSystemPromptSections``：mid-conversation system message 的 section 补丁。"""
    patch: Dict[str, Optional[str]] = {}
    for name, text in current.items():
        if previous.get(name) != text:
            patch[name] = text
    for name in previous:
        if name not in current:
            patch[name] = None
    return patch or None


__all__ = [
    "DEFAULT_SELECTED_TOOLS",
    "PI_PREAMBLE",
    "PI_TOOL_GUIDELINES",
    "PI_TOOL_SNIPPETS",
    "build_docs_section",
    "build_rules",
    "build_system_prompt",
    "build_system_prompt_sections",
    "build_system_prompt_state",
    "diff_system_prompt_sections",
    "format_skills_for_prompt",
    "get_docs_path",
    "get_examples_path",
    "get_package_dir",
    "get_readme_path",
    "normalize_build_system_prompt_options",
    "render_project_context",
]
