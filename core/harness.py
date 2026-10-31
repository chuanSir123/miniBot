from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from core.console import log
import itertools
import json
import os
import re
import time
from pathlib import Path

from core.compaction import ContextCompactor
from core.transcript import (
    KIND_MEMORY,
    KIND_NOTICE,
    is_internal_user_entry,
    last_meta,
    memory_entry,
)


def last_real_user_message(history):
    """最近一条用户真正输入的条目；纯内部条目（记忆/提示）不算。"""
    for message in reversed(history or []):
        if message.get("role") != "user":
            continue
        if is_internal_user_entry(message):
            continue
        return message
    return None


def needs_user_prompt_append(history, user_prompt):
    """这轮用户输入是否还需要落库。

    Web 端在发起请求时已先落过一次；记忆条目也以 user 身份跟在后面。
    如果直接看"最后一条 user"，命中的会是记忆条目，于是同一句话被追加第二次
    —— 这就是切换会话时命令打印两遍的原因。
    """
    last_user = last_real_user_message(history)
    if last_user is None:
        return True
    return str(last_user.get("content") or "") != str(user_prompt or "")


def _recall_payload(memory_recall, context_prompt, context_events):
    """把调用方给的记忆召回转成 memory_entry() 认识的形状。

    新调用方（main.py / webui）直接传结构化的 ``{facts, facts_hash, events[], related[]}``；
    老调用方只有两段字符串，就按内容哈希去重——内容不变就不会重复追加进历史。
    """
    if memory_recall:
        return memory_recall
    facts = (context_prompt or "").strip()
    events = (context_events or "").strip()
    if not facts and not events:
        return None
    recall = {}
    if facts:
        recall["facts"] = facts
    if events:
        import hashlib

        recall["events"] = [{"id": hashlib.sha1(events.encode("utf-8")).hexdigest()[:16], "text": events}]
    return recall

# 核心工具定义 (OpenAI Tool Specification 格式)
HARNESS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "执行本地终端命令。Windows 固定使用 PowerShell，请使用 PowerShell 语法；列出文件名用 Get-ChildItem -LiteralPath '路径' -Name，不要使用 CMD 的 dir /b。确需 CMD 时显式调用 cmd.exe /c。非 Windows 使用系统 shell。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令行命令，如 'git status', 'dir', 'python test.py'",
                    },
                    "timeout_sec": {
                        "type": "number",
                        "description": "首阶段等待超时秒数，默认 15 秒",
                        "default": 15,
                    },
                    "background": {
                        "type": "boolean",
                        "description": "是否在后台异步运行（耗时较长、非阻塞的命令建议设为 true）",
                        "default": False,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件内容，支持指定行范围，防止长文件过载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件绝对路径或相对路径",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号 (从1开始)",
                        "default": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最多读取行数",
                        "default": 120,
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入或覆盖本地文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "content": {
                        "type": "string",
                        "description": "要写入的文件文本内容",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确替换现有文件中的代码或文本片段。target 必须与现有文件中的目标代码段完全严格匹配，replacement 为替换的新代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "target": {"type": "string", "description": "要被替换的原始精确文本块"},
                    "replacement": {"type": "string", "description": "替换后的新内容"},
                },
                "required": ["file_path", "target", "replacement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screen",
            "description": "捕获桌面屏幕或在浏览器中打开渲染网页并截屏核验。用于前端网页、HTML、SVG、小游戏、动画交互等代码的视觉闭环验证，支持虚拟时间多帧采样对比与自动生成动态 GIF。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "截屏或核验观察的原因"},
                    "file_path": {"type": "string", "description": "要在浏览器中打开渲染并截屏核验的本地 HTML/SVG 文件路径（相对当前工作空间或绝对路径）。若未提供且刚生成了前端文件，系统将自动关联"},
                    "url": {"type": "string", "description": "要在浏览器中打开渲染并截屏核验的 URL 地址（如 http://localhost:8000）"},
                    "frames": {"type": "integer", "description": "截图采样帧数。静态截图默认为 1；对于小游戏、动画、交互式页面或计时器，建议设置为 3~5 帧以多帧对比验证动态有效性并生成 GIF 动画"},
                    "interval_seconds": {"type": "number", "description": "多帧采样时的虚拟时间间隔秒数，默认 0.5 秒"},
                    "width": {"type": "integer", "description": "浏览器渲染视口宽度，默认 1280"},
                    "height": {"type": "integer", "description": "浏览器渲染视口高度，默认 800"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_skill",
            "description": "执行内置或用户技能。脚本型技能传 arguments 执行；指令型技能（只有SKILL.md）先不带 arguments 调用获取完整说明，再按说明执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能名称"},
                    "arguments": {"type": "object", "description": "技能参数JSON对象，例如 {\"prompt\":\"猫娘\",\"width\":480}，不要序列化为字符串"},
                    "background": {"type": "boolean", "description": "是否在后台异步运行", "default": False},
                    "timeout_sec": {"type": "number", "description": "首阶段等待超时秒数，默认按技能配置"},
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_task",
            "description": "查询后台任务的执行状态、运行耗时与最新日志输出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "后台任务ID"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wait_task",
            "description": "同步等待正在后台运行的任务完成。若在指定超时时间内未完成，将返回当前进度和最新捕获日志，任务继续在后台运行，绝不强杀。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "后台任务ID"},
                    "timeout_sec": {"type": "number", "description": "本次同步等待超时秒数，默认 15 秒", "default": 15}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "主动取消并终止正在运行的后台任务进程树。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要取消的后台任务ID"}
                },
                "required": ["task_id"]
            }
        }
    },
]


def _tool(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


HARNESS_TOOLS += [
    _tool("read_chat_history", "读取当前群/私聊的原始聊天记录，包括未触发回复的消息；摘要中的旧消息可用此工具回查，不跨会话读取。",
          {"before_id": {"type": "integer"}, "limit": {"type": "integer"}}),
    _tool("manage_permissions", "结合当前主人回复、引用消息和历史理解授权/撤销意图。例如引用某人申请联网权限后主人回复‘可以’‘给他开一下’，应据引用的真实发送者QQ和所申请能力执行授权，无需固定关键词。仅当前实际发言人是配置主人时能grant/revoke；引用主人、群友自称主人不授予身份。目标QQ可从引用消息结构化发送者信息、@或明确历史记录获取，不凭昵称猜测。主人只是讨论/拒绝申请时不授权，目标/能力不明确时询问。群内默认当前群，私聊默认private；只有明确跨群/全局要求才填all。单个技能用skills，工具用tools，全部工具填tools=['*']；duration_minutes=0长期。",
          {"action": {"type": "string", "enum": ["grant", "revoke", "list"]},
           "user_id": {"type": "string", "description": "目标QQ号；只有主人明确要求本群全体成员时才使用*，并限定具体群"}, "chat_scope": {"type": "string"},
           "tools": {"type": "array"}, "skills": {"type": "array"}, "duration_minutes": {"type": "number"}}, ["action"]),
    _tool("list_media", "查询当前会话近期媒体引用（图片/视频/音频）及来源。修改上一张/刚才的图时先查询；优先选当前上传或引用消息的媒体，不要猜测media_ref。不会返回Base64。",
          {"limit": {"type": "integer"}, "message_id": {"type": "string"}}),
    _tool("get_media", "查看当前会话指定媒体的元数据；图片额外返回预览。不返回Base64文本；编辑图片时复用同一个media_ref。",
          {"media_ref": {"type": "string"}}, ["media_ref"]),
    _tool("register_media", "把已确认存在的本机媒体文件（图片/视频/音频，如 .png .jpg .mp4 .webm .mp3 .wav .m4a）登记为当前会话media_ref，以便编辑、发送到QQ或后续引用。相对路径按当前Harness工作空间解析。",
          {"file_path": {"type": "string"}}, ["file_path"]),
    _tool("activate_skill", "重新扫描技能目录并重新加载技能清单（技能目录变化通常会被自动发现，一般无需调用）。",
          {"skill_name": {"type": "string"}}, ["skill_name"]),
    _tool("restart_bot", "重启 MiniBot 本体进程：优雅停机后由启动器自动重新拉起，用于改了配置/技能后需要立刻生效的场景。"
                         "本条回复发出之后才会真正重启；只有主人（或被主人明确授权的人）能执行，群友要求重启时不要谎称已经重启。",
          {"reason": {"type": "string", "description": "重启原因，写进日志，一句话即可"}}),
    _tool(
        "schedule_reminder",
        "创建持久化提醒或条件跟进；Unix秒时间戳。条件支持 idle、active、error、error_cleared、window:文本。",
        {
            "trigger_at": {"type": "number"},
            "message": {"type": "string"},
            "condition": {"type": "string"},
        },
        ["trigger_at", "message"],
    ),
    _tool(
        "read_history",
        "读取当前会话被压缩归档的原始消息，用于核对细节和约束。",
        {"after_id": {"type": "integer"}, "limit": {"type": "integer"}},
    ),
    _tool(
        "list_skills",
        "查询当前系统中所有已注册和可用的技能列表、功能说明及调用参数规范。",
        {
            "query": {
                "type": "string",
                "description": "可选搜索或过滤关键词，留空则列出所有可用技能",
            }
        },
    ),
    _tool("list_reminders", "查询当前会话提醒", {}),
    _tool(
        "update_reminder",
        "取消或改期当前会话提醒",
        {
            "task_id": {"type": "integer"},
            "cancel": {"type": "boolean"},
            "trigger_at": {"type": "number"},
        },
        ["task_id"],
    ),
    _tool(
        "remember_fact",
        "仅保存用户明确要求记住的事实",
        {"key": {"type": "string"}, "value": {"type": "string"}},
        ["key", "value"],
    ),
    _tool("forget_fact", "删除当前身份的事实", {"key": {"type": "string"}}, ["key"]),
    _tool(
        "notification_feedback",
        "记录主动提醒反馈，mute暂停主动打扰，resume恢复，useful/not_useful记录质量。",
        {
            "value": {
                "type": "string",
                "enum": ["mute", "resume", "useful", "not_useful"],
            }
        },
        ["value"],
    ),
    _tool(
        "undo_memory",
        "撤销最近一次记忆或事实的修改/合并，恢复上一次的有效状态。",
        {},
    ),
    _tool(
        "manage_goal",
        "长程任务目标管理（创建、查询、推进步骤、完成、提议修订）。",
        {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "update_step", "set_status", "propose"],
                "description": "操作类型：create创建, list查询, get详情, update_step更新步骤, set_status修改状态, propose提议修订",
            },
            "goal_id": {"type": "string", "description": "目标唯一ID，如 g-a1b2c3d4"},
            "title": {"type": "string", "description": "目标标题 (create必填)"},
            "category": {"type": "string", "description": "目标类别: coding, learning, project, health, work, life, other"},
            "steps": {"type": "array", "items": {"type": "string"}, "description": "步骤列表，如 ['排查问题', '编写测试', '提交代码']"},
            "cadence": {"type": "string", "description": "推进/提醒周期，如 'daily 09:00', 'weekdays 08:30', 'on_active'"},
            "step_id": {"type": "integer", "description": "步骤编号 (从1开始)"},
            "step_status": {"type": "string", "enum": ["pending", "in_progress", "done", "blocked", "skipped"], "description": "步骤新状态"},
            "evidence": {"type": "string", "description": "步骤完成证据或进展说明"},
            "target_date": {"type": "string", "description": "截止日期 (可选)"},
        },
        ["action"],
    ),
]
# 工具路由提示、行为准则与技能目录全部归到 core/context_builder.py，
# 按「路由提示 / 行为准则 / 技能目录」三类组织：
# 能力与参数只写在 HARNESS_TOOLS 的 schema 里，行为约束只写在 TOOL_GUIDELINES 里，
# 工具不在场时对应准则自动消失。同一句话只有一个权威出处。


def build_structured_system_prompt(
    scope="main",
    channel=None,
    custom_sections=None,
    extra_rules=None,
    selected_tools=None,
    skills=None,
):
    """兼容入口：委托给 core.context_builder 的 section 装配并返回渲染好的文本。

    新增的 ``selected_tools`` / ``skills`` 让调用方显式声明工具集与技能目录；
    不传时按全集渲染，与旧行为一致。
    """
    from core.context_builder import SYSTEM_PROMPT_CACHE

    if isinstance(custom_sections, dict) and "available_skills" in custom_sections and skills is None:
        # 旧调用方把技能目录塞在 custom_sections 里，转回结构化的 skills 参数以便压缩签名。
        try:
            import json as _json

            skills = _json.loads(custom_sections["available_skills"])
            custom_sections = {k: v for k, v in custom_sections.items() if k != "available_skills"}
        except (TypeError, ValueError):
            pass

    text, _fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope=scope,
        channel=channel,
        selected_tools=selected_tools,
        skills=skills,
        extra_rules=extra_rules,
        custom_sections=custom_sections,
    )
    return text


BASE_RUNTIME_RULES = build_structured_system_prompt()


def assess_command(command: str, is_tainted: bool = False) -> dict:
    """
    Dynamic security assessment for shell commands.
    1. Blocks destructive root/system commands (e.g. format, dangerous recursive root deletions).
    2. Taint tracking enforcement: If session is tainted by reading sensitive data (keys/creds/configs),
       strictly block outbound network egress to prevent indirect prompt injection exfiltration.
    """
    cmd_lower = command.lower()

    # Destructive system commands
    destructive_patterns = [
        r"\bformat\s+[a-z]:",
        r"\bdiskpart\b",
        # 拦截针对根盘符或系统关键目录的破坏性删除，但允许在工作区内部清理缓存
        r"\b(?:rmdir|rd|del|remove-item)\b.*?[a-z]:\\(?:\s*['\"]?$|\s*(?:-force|-recurse|-r|-s|-q))",
        r"\b(?:rmdir|rd|del|remove-item)\b.*?[a-z]:\\(?:windows|program\s+files|programdata|recovery|boot)\b",
        r"\bshutdown\b.*?(?:/[srf]|-s|-r)",
        r"\btaskkill\b.*?/im\s+(?:csrss|lsass|smss|svchost)\.exe",
    ]
    for pattern in destructive_patterns:
        if re.search(pattern, cmd_lower):
            return {
                "allowed": False,
                "reason": f"安全拦截：命令包含高危系统破坏性指令模式 ({pattern})，已拒绝执行",
            }

    # Taint tracking egress inspection
    if is_tainted:
        egress_patterns = [
            r"\bcurl\b",
            r"\bwget\b",
            r"\binvoke-webrequest\b",
            r"\biwr\b",
            r"\binvoke-restmethod\b",
            r"\birm\b",
            r"\bssh\b",
            r"\bscp\b",
            r"\bgit\s+push\b",
            r"\bnc\b",
            r"\bncat\b",
            r"\bftp\b",
        ]
        for pattern in egress_patterns:
            if re.search(pattern, cmd_lower):
                return {
                    "allowed": False,
                    "reason": f"【安全哨兵·污点追踪】当前会话已读取敏感数据（如配置文件或私密文件），已触发污点防护。禁止在受污染会话中执行外发网络命令 ({pattern})，防止潜在数据外泄或提示词注入攻击。如需执行，请通过全新会话运行。",
                }

    return {"allowed": True, "reason": ""}


# ==========================================================
# 重复调用守卫
# ==========================================================
# 只读工具：同样的参数再执行一次，结果必然一样。模型拿到一样的内容不会前进，
# 只会再读一次——pelican_bike.html 连续读七遍就是这种空转。
# 有副作用的工具（写文件、跑命令、截图）不在此列：改完文件重跑同一条测试命令
# 是完全正常的操作，靠参数相同去拦会误伤。
# 只有"结果完全由参数决定"的工具才配拦截：read_file 有文件版本戳兜底，
# get_media 按 media_ref 取同一个文件。list_files / read_history 的结果会随
# 新文件、新消息变化，按参数相同去拦会把"确认一下刚才写进去没有"挡掉。
STABLE_READ_TOOLS = frozenset({"read_file", "get_media"})

# 第 2 次：内容照给，但附一句"和上一步一样"；第 3 次起：不再执行，直接退回提示。
DUPLICATE_NOTICE_AT = 2
DUPLICATE_BLOCK_AT = 3
# 一直重复到第 6 次说明提示没用，直接收尾，避免 100 轮上限里空转几百秒。
DUPLICATE_ABORT_AT = 6


def call_signature(name: str, args: dict) -> str:
    """把一次调用压成可比较的字符串：工具名 + 排序后的参数。"""
    try:
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(args)
    return f"{name}:{payload}"


def diff_for_model(diff_text: str, is_new: bool, max_hunks: int = 6) -> str:
    """工具结果里回给模型的那一点 diff —— 只有位置，没有内容。

    参考实现：``write`` 只回 ``Successfully wrote to <path>``，``edit`` 只回
    ``Successfully replaced N block(s) in <path>.``，完整 diff 放在 ``details``
    里给 UI 渲染，**从不进入 provider payload**。理由是这份内容本来就在模型的
    工具调用参数里（write 的 content、edit 的 replacement），回传等于同一份文本
    在上下文里存两遍：实测一次 9.8KB 的页面，参数 10.8KB + diff 11.6KB。

    这里略宽松：编辑时给 hunk 头（``@@ -12,3 +12,4 @@``），让模型知道
    "改了哪几处、大概在哪一行"，但不复述任何被改的内容。
    """
    if not diff_text or is_new:
        return ""
    hunks = [ln for ln in diff_text.splitlines() if ln.startswith("@@")]
    if not hunks:
        return ""
    shown = hunks[:max_hunks]
    if len(hunks) > max_hunks:
        shown.append(f"…共 {len(hunks)} 处改动")
    return "\n".join(shown)


def file_version(path: str, scope: str = "main") -> str:
    """文件版本戳：文件改过就允许重读，没改才算重复。"""
    if not path:
        return ""
    try:
        from core.workspace import resolve_file
        stat = resolve_file(path, scope).stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return ""


def guard_repeated_call(name, args, signature, seen, scope, step):
    """判断这次调用是否在空转。返回 (拦截结果或 None, 给模型的提醒)。

    seen 是本次 run 内的字典，键为签名。所有工具都计数（收尾时据此提醒），但只有
    STABLE_READ_TOOLS 会被真正拦住；记录里带文件版本戳，所以"改完文件再读同一个
    文件"算新的一次，不受影响。
    """
    version = file_version(str(args.get("file_path") or ""), scope) if name in STABLE_READ_TOOLS else ""
    record = seen.get(signature)
    if record and record.get("version") == version:
        record["count"] += 1
    else:
        record = {"count": 1, "version": version, "step": step, "last": {}}
        seen[signature] = record
    if name not in STABLE_READ_TOOLS:
        return None, ""
    count = record["count"]
    if count < DUPLICATE_NOTICE_AT:
        return None, ""
    last = record.get("last") or {}
    follow = ""
    if last.get("next_start"):
        follow = f"要看后续内容请传 start_line={last['next_start']}，要换角度看请用其他工具。"
    if count < DUPLICATE_BLOCK_AT:
        return None, (f"这次调用与第 {record['step'] + 1} 步完全相同，结果也一样，"
                      f"不会带来新信息。改用不同参数或直接基于已有内容作答。{follow}")
    return {
        "ok": False,
        "duplicate": True,
        "error": (
            f"同一调用已重复 {count} 次且结果完全相同（文件未修改），已阻止再次执行。"
            f"重复同样的调用不会得到新结果：{follow or '请换参数、换工具，或基于已有内容直接作答。'}"
        ),
        "same_as_step": record["step"] + 1,
    }, ""


class AgentHarness:
    def __init__(self, brain, jev, memory, perception, skill_manager, max_turns=100):
        self.brain, self.jev, self.memory = brain, jev, memory
        self.perception, self.skill_manager = perception, skill_manager
        self.max_turns = max_turns
        self.on_completed = None
        # 重启回调：(reason, session_id) -> bool，由宿主（main.MiniBotApp）注入。
        # 没有它时 restart_bot 工具会明确报"当前环境不支持重启"，而不是假装重启。
        self.on_restart = None
        threading = __import__("threading")
        self._stop = threading.Event()
        self._local = threading.local()
        self._active = {}
        self._active_lock = threading.Lock()
        self._tainted_sessions = set()
        self._tainted_lock = threading.Lock()
        self._steering_inbox = {}
        self._steering_lock = threading.Lock()
        self._interrupt_stream = {}
        self._active_goals = {}

    def mark_tainted(self, session_id: str):
        with self._tainted_lock:
            self._tainted_sessions.add(session_id)

    def is_tainted(self, session_id: str) -> bool:
        with self._tainted_lock:
            return session_id in self._tainted_sessions

    def clear_taint(self, session_id: str):
        with self._tainted_lock:
            self._tainted_sessions.discard(session_id)

    def stop(self):
        self._stop.set()
        with self._active_lock:
            for event in self._active.values():
                event.set()

    def is_session_active(self, session: str) -> bool:
        with self._active_lock:
            return session in self._active

    def _cancelled(self) -> bool:
        """统一判断当前执行线程是否应停止（全局退出 / 会话级停止）。"""
        if self._stop.is_set():
            return True
        event = getattr(self._local, "cancel", None)
        return bool(event is not None and event.is_set())

    def _abort_reply(self, session_id: str = "") -> str:
        """用户主动停止：立即返回，不写入历史，避免把"任务已取消"当成模型回答存进上下文。"""
        log("Harness", "cancelled", session=session_id,
            elapsed_ms=round((time.monotonic() - getattr(self._local, "started_at", time.monotonic())) * 1000))
        return "任务已取消。"

    def get_active_goal(self, session: str) -> str:
        with self._active_lock:
            return self._active_goals.get(session, "")

    def inject_steering(self, session: str, steering_text: str) -> bool:
        """Inject mid-run direction guidance into an ongoing Harness execution."""
        with self._active_lock:
            if session not in self._active:
                return False
            event = self._interrupt_stream.get(session)
            if event:
                event.set()
        with self._steering_lock:
            self._steering_inbox.setdefault(session, []).append(steering_text)
        log("Harness", "steering.injected", session=session, text=steering_text)
        return True

    def cancel_session(self, session):
        with self._active_lock:
            event = self._active.get(session)
            if event:
                event.set()
            return bool(event)

    def review_completion(
        self,
        reply: str,
        user_prompt: str = "",
        trace: Optional[List[Dict]] = None,
        task_state: Optional[Dict] = None,
        scope: str = "main",
        channel: Optional[str] = None,
        decision_context: Optional[Dict] = None,
        task_goal: str = "",
        continuing: bool = False,
        input_images: Optional[List] = None,
        on_status_change: Optional[Callable[[str], None]] = None,
        session_id: str = "",
    ) -> Tuple[Optional[bool], str]:
        """
        Closed-loop verification of task completion and code self-testing.
        Returns (is_complete, feedback_message).
        """
        from core.workspace import resolve_file
        from core.config import load_config

        trace = trace or []
        task_state = task_state or {}
        decision_context = decision_context or {}

        cfg = load_config()

        # 2. 确定性门禁先过一遍：这些是"查记录"，不是"猜意图"
        # 本轮真正改动过的文件（命令/技能造成的源码变化也算）。续作时带上的历史
        # 改动不参与门禁——用户发一句"继续"不该被要求重新测试和截图。
        this_turn_files = []
        for t in trace:
            for p in t.get("result", {}).get("changed_files", []):
                if p not in this_turn_files and resolve_file(p, scope).is_file():
                    this_turn_files.append(p)
            if t.get("tool") in ("write_file", "edit_file") and t.get("result", {}).get("ok"):
                p = t.get("result", {}).get("path") or t.get("arguments", {}).get("file_path") or ""
                if p and p not in this_turn_files:
                    this_turn_files.append(p)

        # 交给放行判定的是"这轮和上一轮一起动过什么"，门禁用上面那份本轮清单。
        modified_files = list(this_turn_files)
        if continuing:
            for fc in task_state.get("file_changes", []):
                p = fc.get("path") or ""
                if p and p not in modified_files:
                    modified_files.append(p)

        from core.verification import CODE_SUFFIXES, WEB_SUFFIXES
        code_touched = any(Path(p).suffix.lower() in CODE_SUFFIXES | WEB_SUFFIXES
                           for p in this_turn_files)
        if not cfg.get("runtime", {}).get("verify_completion", True):
            if code_touched:
                return False, "代码已修改，但完成核验服务不可用或已关闭；不能确认功能与目标一致。请恢复核验配置后重试。"
            return None, ""

        if not (reply or "").strip():
            return False, "验证后需提供简短交付说明，不能返回空回复。"

        # 是否需要截图、要不要重新跑测试，都不再提前预测：本轮确实改了可见页面
        # 就要求渲染过一次，确实改了代码就要求跑出断言结果，没改就不要求。
        # 开场那一刻没有任何工具记录，"这轮会不会改页面"只能靠用户措辞猜；收尾时
        # 这个问题有确定答案。
        if not cfg.get("runtime", {}).get("verify_completion", False):
            return True, ""

        if not (reply or "").strip():
            return False, "验证后需提供简短交付说明，不能返回空回复。"

        from core.verification import completion_gate
        gate_feedback = completion_gate(
            trace, this_turn_files, scope,
            motion_required=bool(cfg.get("runtime", {}).get("verify_motion", True)))
        if gate_feedback:
            return False, gate_feedback

        pure_text_turn = not trace and not modified_files and not continuing and not input_images
        if pure_text_turn and not cfg.get("runtime", {}).get("verify_chat", False):
            return None, ""

        return True, ""

        # 纯文本轮次：没有任何外部操作可供核验，普通闲聊不为它做检查。
        pure_text_turn = not trace and not modified_files and not continuing and not input_images
        if pure_text_turn and not cfg.get("runtime", {}).get("verify_chat", False):
            return None, ""

        # 能被程序核对的项目到这里都核对完了。交付"对不对得上目标"不再交给分类器：
        # 它判不准的代价是用户收到一个坏交付，判得准也要多等一两秒，两头都不划算。
        # 这一步只保证一件事——交出去的东西不缺证据。
        return True, ""

    def review(self, reply: str, user_prompt: str = "", tool_history: Optional[List[Dict]] = None, **kwargs) -> Dict:
        """Convenience method for tests and external inspection."""
        trace = tool_history or kwargs.pop("trace", None) or []
        ok, feedback = self.review_completion(reply, user_prompt=user_prompt, trace=trace, **kwargs)
        return {"ok": ok is True, "status": "passed" if ok is True else "failed" if ok is False else "unverified", "feedback": feedback}

    @staticmethod
    def _recent_messages(history, limit=10, chars=600):
        """最近几条"人话"消息，给需要判断上下文承接的 Jev 调用。

        Jev 判断"继续还是新任务"、"该不该调这个技能"只需要最近的对话走向，
        整份 task_state 的 diff 全文与工具流水反而把它拖到 8s 以上。
        """
        out = []
        for item in history or []:
            if item.get("role") not in ("user", "assistant"):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            out.append({"role": item["role"], "text": content[:chars]})
        return out[-limit:]

    @staticmethod
    def _task_brief(task_state):
        """任务摘要：关系类型判断用不到 diff 全文和工具流水。"""
        from core.decision_context import BRIEF_TASK_KEYS
        if not isinstance(task_state, dict):
            return {}
        return {k: v for k, v in task_state.items() if k in BRIEF_TASK_KEYS}

    def run(self, *args, **kwargs):
        session = kwargs.get("session_id", args[4] if len(args) > 4 else "local:pet")
        scope = kwargs.get("scope") or "main"
        user_message_id = kwargs.get("user_message_id")
        # 只读/无工具的一轮不会产生文件变更，跳过整轮清单扫描省 IO。
        track_files = not kwargs.get("readonly") and kwargs.get("allow_tools", True) is not False
        event = __import__("threading").Event()
        with self._active_lock:
            self._active[session] = event
            self._active_goals[session] = kwargs.get("user_prompt", args[0] if args else "")
            self._interrupt_stream[session] = __import__("threading").Event()
        with self._steering_lock:
            self._steering_inbox[session] = []
        self._local.cancel = event
        # 权限身份（当前发送者QQ号）随本轮任务走，工具执行时统一从这里取，
        # 不从模型输出或会话文本里推断。
        self._local.actor = kwargs.get("actor") or ""
        started = time.monotonic()
        self._local.started_at = started
        self._local.timings = {"route_ms": 0, "model_ms": 0, "tool_ms": 0, "review_ms": 0,
                               "model_calls": 0, "review_calls": 0}
        log("Harness", "start", session=session)
        # 轮次文件台账：技能/命令直接写盘的 mp4、gif、图片不经过 write_file，
        # 只能靠「轮初清单 + 轮末对比」才能被回滚看见。
        turn_tracked = False
        if track_files:
            try:
                from core.checkpoint import get_checkpoint_manager
                get_checkpoint_manager().begin_turn(session, scope, message_id=user_message_id)
                turn_tracked = True
            except Exception as exc:
                log("Harness", "turn.track_begin_error", session=session, error=str(exc)[:200])
        try:
            # user_message_id 只用于把本轮文件变更归属到那条用户消息，不传给 _run。
            run_kwargs = {k: v for k, v in kwargs.items() if k != "user_message_id"}
            reply = self._run(*args, **run_kwargs)
            log(
                "Harness",
                "complete",
                session=session,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                timings=self._local.timings,
                reply=reply,
            )
            return reply
        except Exception as exc:
            log("Harness", "error", session=session, error=str(exc))
            raise
        finally:
            if turn_tracked:
                try:
                    from core.checkpoint import get_checkpoint_manager
                    stats = get_checkpoint_manager().end_turn(session)
                    if stats.get("created") or stats.get("modified") or stats.get("deleted"):
                        log("Harness", "turn.files", session=session, **stats)
                except Exception as exc:
                    log("Harness", "turn.track_end_error", session=session, error=str(exc)[:200])
            self._local.status = None
            self._local.readonly = False
            self._local.review_feedback = None
            self._local.actor = ""
            # 清掉本线程的取消信号，避免同一 worker 线程的下一个任务误判为"已取消"。
            self._local.cancel = __import__("threading").Event()
            try:
                state = self.memory.get_task_state(session)
                if state and state.get("status") == "active":
                    state["status"] = "awaiting_input"
                    self.memory.save_task_state(session, state)
            except Exception as exc:
                log("Harness", "checkpoint.error", session=session, error=str(exc)[:200])
            with self._active_lock:
                self._active.pop(session, None)
                self._active_goals.pop(session, None)
                self._interrupt_stream.pop(session, None)
            with self._steering_lock:
                self._steering_inbox.pop(session, None)

    def _run(
        self,
        user_prompt,
        context_prompt="",
        on_text_chunk=None,
        on_status_change=None,
        context_events="",
        memory_recall=None,
        tail_context="",
        session_id="local:pet",
        scope="local",
        channel="pet",
        target="",
        allow_tools=True,
        readonly=False,
        input_images=None,
        decision_context=None,
        on_intermediate=None,
        on_tool_call=None,
        on_tool_result=None,
        on_steering_applied=None,
        on_tool_delta=None,
        on_reasoning_chunk=None,
        on_reasoning_complete=None,
        on_text_reset=None,
        model_override=None,
        actor="",
    ):
        from core.config import load_config

        cfg = load_config()
        if not hasattr(self._local, "timings") or not self._local.timings:
            self._local.timings = {"route_ms": 0, "model_ms": 0, "tool_ms": 0, "review_ms": 0, "model_calls": 0, "review_calls": 0}
        if not hasattr(self._local, "cancel"):
            self._local.cancel = __import__("threading").Event()
        self._local.status = on_status_change
        self._local.actor = actor or getattr(self._local, "actor", "") or ""
        history, persisted = self.memory.get_history(session_id)
        if decision_context is None:
            from core.decision_context import build_decision_context
            decision_context = build_decision_context(self.memory, session_id, scope)
        from core.qq_authority import tool_access
        actor = getattr(self._local, "actor", "") or actor
        decision_context = dict(decision_context, channel=channel,
                                local_tools_allowed=bool(tool_access(scope, "execute_command", actor=actor) and not readonly), readonly=readonly)
        # 固定动作与技能路由共用这份精简上下文：完整版 9.5KB 里 5.2KB 是媒体
        # 清单和会话摘要，"这句话能不能用预设动作直接满足"读不到它们。
        from core.decision_context import lite_context
        lite_ctx = lite_context(decision_context)
        previous_checkpoint = self.memory.get_task_state(session_id) or decision_context.get("task_state") or decision_context.get("continuation_checkpoint") or {}
        unfinished = bool(previous_checkpoint.get('original_goal') and previous_checkpoint.get('status') not in ('complete','completed'))
        continuing = False
        if unfinished:
            if self.jev and self.jev.is_configured():
                try:
                    continuing = self.jev.classify_steering_intent(
                        user_prompt,
                        task_state=self._task_brief(previous_checkpoint),
                        messages=self._recent_messages(history),
                    ) == 'steering'
                except Exception:
                    continuing = False
            else:
                continuing = user_prompt.strip().lower() in ('继续', '接着', 'continue', '/continue')
        task_goal = previous_checkpoint.get("original_goal", user_prompt) if continuing else user_prompt
        task_state = dict(previous_checkpoint) if continuing else {"last_tools": [], "artifacts": []}
        # 旧版本会把"开场定下的核验契约"持久化进 task_state；这个契约已经废弃
        # （核验要求由收尾时的确定性门禁按本轮真实改动现算），清掉避免旧键被当成
        # 本轮验收标准喂给判定。
        task_state.update(original_goal=task_goal, latest_request=user_prompt, status="active")
        task_state.pop('verification_requirements', None)
        self.memory.save_task_state(session_id, task_state)
        from core.decision_context import compact_task_state
        decision_context["task_state"] = compact_task_state(
            {**task_state, "last_tools": task_state.get("last_tools", [])[-8:],
             "artifacts": task_state.get("artifacts", [])[-32:]})
        compactor = ContextCompactor(self.brain, self.memory, session_id,
                                     cancelled=self._cancelled)
        compactor.summary = persisted
        # 摘要在一轮开头快照一次：后台压缩线程即使在本轮跑完，也要等下一轮才生效。
        # 否则 assemble() 会在两次模型请求之间突然换掉前缀，把刚命中的缓存打穿。
        compactor.begin_turn()

        def append(message):
            saved = self.memory.append_message(session_id, message)
            history.append(saved)
            return saved

        pending = {}
        for previous in history:
            for call in previous.get("tool_calls", []):
                pending[call["id"]] = call
            if previous.get("role") == "tool":
                pending.pop(previous.get("tool_call_id"), None)
        for call_id in pending:
            append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": "上次运行中断，执行结果未知，先验证状态再继续。",
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        # 记忆召回的位置：作为**追加条目**进历史，而不是每轮重画一段尾部。
        # 重画的代价是这段内容每轮都命中不了缓存（facts 1881 字符 ≈ 1130 token/轮）；
        # 追加之后它成为前缀的一部分，此后每轮整段命中。内容没变就不追加。
        recall = _recall_payload(memory_recall, context_prompt, context_events)

        # 顺序必须是"用户输入 → 记忆条目"。
        # 反过来的话（记忆先入历史）去重会命中记忆条目，把同一句话再追加一遍，
        # Web 端切会话时就会看到用户命令打印两次（旧 bug）。
        if needs_user_prompt_append(history, user_prompt):
            append({"role": "user", "content": user_prompt})
        if recall:
            entry = memory_entry(recall, last_meta(history, KIND_MEMORY))
            if entry:
                append(entry)
        def append_notice(text):
            """执行期提示：以 user 身份追加，绝不新增 system 段。

            vLLM/Gemma 只保证首条 system 的语义，中途插 system 会被各网关以不同方式
            改写；统一落成带固定前缀的 user 条目，投影结果逐字节可预测。
            """
            return append({"role": "user", "content": text, "meta": {"kind": KIND_NOTICE}})  # noqa: E501

        def apply_pending_steerings():
            with self._steering_lock:
                pending_steers = list(self._steering_inbox.get(session_id, []))
                self._steering_inbox[session_id] = []
            if not pending_steers:
                return False
            combined = "\n".join(pending_steers).strip()
            if not combined:
                return False
            steer_message = {
                "role": "user",
                "content": f"【用户实时引导与方向修正】\n{combined}\n（注意：这是用户针对正在执行的任务发出的中途引导与方向修正指令，请立即依据此修正后续执行策略，停止错误或冲突的操作！）",
                "is_steering": True,
            }
            append(steer_message)
            task_state["latest_request"] = combined
            self.memory.save_task_state(session_id, task_state)
            if on_status_change:
                on_status_change(f"已应用实时引导：{combined[:24]}…")
            if on_intermediate:
                try:
                    on_intermediate(f"[实时方向修正] {combined}")
                except Exception:
                    pass
            if on_steering_applied:
                try:
                    on_steering_applied(combined)
                except Exception:
                    pass
            log("Harness", "steering.applied", session=session_id, steering=combined)
            return True
        from core.context_builder import (
            SYSTEM_PROMPT_CACHE,
            compact_checkpoint,
            render_clock,
            render_runtime_context,
            render_tail_context,
        )
        from core.workspace import PROJECT_ROOT, workspace_for

        goals_summary = ""
        if hasattr(self.memory, "goals"):
            goals_summary = self.memory.goals.format_goals_summary(scope) or ""
        # 慢变部分只剩目标：记忆 facts/events 已经作为条目进了历史（见上面的
        # memory_entry），留在尾部只会每轮重算一遍。时钟交给 render_tail_context
        # 排到整条尾部的最末——顺序错了的话，时钟一变就把它后面的内容全部打穿。
        runtime_context = render_runtime_context(
            scope=scope,
            goals=goals_summary,
            include_clock=False,
        )
        # 时钟在一轮内固定：tool loop 可能跑几分钟，每轮重新取时间会让尾部最后一块
        # 白白作废。同一轮里几十秒的差异对模型没有意义，跨轮才需要更新。
        tail_clock = render_clock()
        trace = []
        image = None
        # Discovery is complete; all authority checks happen in _execute_tool.
        permitted = {t["function"]["name"] for t in HARNESS_TOOLS}
        self._local.readonly = readonly
        tools = list(HARNESS_TOOLS)
        # 技能目录只随技能集合变化，用稳定排序保证同一集合渲染结果逐字节一致。
        # 它属于冻结 section：进 system 前缀能被整段命中；放进尾部则每轮工具调用都要
        # 重新 prefill（实测约 2.4k tokens，直接贡献数秒 TTFT）。
        catalog = self.skill_manager.describe("", stable=True)
        from core.qq_authority import permission_summary
        decision_context["execution_permissions"] = permission_summary(scope, actor)
        system, system_fp = SYSTEM_PROMPT_CACHE.render(
            scope=scope,
            channel=channel,
            selected_tools=[t["function"]["name"] for t in tools],
            skills=catalog,
        )

        reviews = 0
        review_retries = 0
        connection_retries = 0

        def stream_reset(reason):
            """已展示的草稿被作废（重试/改写）时通知界面，避免新旧文本混在一起。"""
            if not on_text_reset:
                return
            try:
                on_text_reset(reason)
            except Exception as exc:
                log("Harness", "stream.reset_error", session=session_id, error=str(exc)[:200])

        def review(reply):
            nonlocal reviews
            reviews += 1
            review_started = time.monotonic()
            try:
                complete, feedback = self.review_completion(
                    reply=reply,
                    user_prompt=user_prompt,
                    trace=trace,
                    task_state=task_state,
                    scope=scope,
                    channel=channel,
                    decision_context=decision_context,
                    task_goal=task_goal,
                    continuing=continuing,
                    input_images=input_images,
                    on_status_change=on_status_change,
                    session_id=session_id,
                )
                self._local.review_feedback = feedback
                return complete
            finally:
                self._local.timings["review_ms"] += round((time.monotonic() - review_started) * 1000)
                self._local.timings["review_calls"] += 1

        def finish_turn(reply):
            """一轮结束决策。

            默认模式（verify_completion=False）下，模型输出文本且无 tool calls 时自然完成交付；
            绝不强制作废用户可见回复，不强行塞入功能测试报错打回重试。
            """
            nonlocal review_retries, tools
            cfg = load_config()
            if not cfg.get("runtime", {}).get("verify_completion", False):
                task_state["verification"] = {"status": "passed", "feedback": ""}
                task_state["status"] = "complete"
                self.memory.save_task_state(session_id, task_state)
                return "end", reply

            verified = review(reply)
            if verified is False and review_retries < int(cfg.get("runtime", {}).get("completion_retries", 1)):
                review_retries += 1
                tools = available_tools
                feedback = getattr(self._local, "review_feedback", None) or (
                    "上一段回复缺少可核对的证据。请按提示补齐动作；不得重复已成功的有副作用操作。"
                    "无法继续时明确缺少条件，不要宣称完成。")
                self._local.review_feedback = None
                stream_reset("交付前核验未通过，上一段回复已作废")
                append_notice(feedback)
                return "continue", None
            if verified is False:
                log("Harness", "completion.unverified", session=session_id,
                    reason="核验重试已耗尽；阻止未通过结果被当成成功交付", retries=review_retries)
                feedback = getattr(self._local, "review_feedback", "") or "未取得充分完成证据。"
                if not (channel in ("qq_private", "qq_group") and (reply or "").strip()):
                    reply = "本次任务未通过验证，尚未完成。\n" + feedback
            task_state["verification"] = {
                "status": "passed" if verified is True else "failed" if verified is False else "unverified",
                "feedback": getattr(self._local, "review_feedback", "")}
            task_state["status"] = "complete" if verified is not False else "unverified"
            self.memory.save_task_state(session_id, task_state)
            return "end", reply

        def before_tool_call(name, args, signature):
            """执行前的唯一拦截点。

            目前只有一类拦截：同一只读调用以完全相同的参数反复执行。之所以收敛成
            一个函数，是因为散在循环里的拦截点总会漏掉新加的工具——单点拦截
            新增工具时不用回头改循环。
            """
            blocked, notice = guard_repeated_call(
                name, args, signature, repeat_calls, scope, len(trace))
            return blocked, notice
        # 固定动作是进入执行前的快捷入口，不参与任何执行期判定：只问"这句话
        # 能不能被预设参数完整满足"（发最后一张图之类），满足就省一次模型往返。
        route_started = time.monotonic()
        fast_tool = None
        from core.fast_actions import resolve_fixed_action
        if (cfg["runtime"].get("jev_noarg_enabled", True) and not readonly
                and self.jev and self.jev.is_configured()
                and hasattr(self.jev, "select_fixed_action")):
            # 固定动作只问"这句话能不能被预设参数完整满足"，加上最近几张图的 ref
            # 就够（"发最后一张图"靠它），不需要整份 9.5KB 上下文。
            media = [m for m in (decision_context.get("recent_media") or [])[:5] if isinstance(m, dict)]
            fast_tool = self.jev.select_fixed_action(user_prompt, context=dict(lite_ctx, recent_media=[
                {k: m[k] for k in ("media_ref", "mime", "label", "sender_qq") if m.get(k)} for m in media]))
            if not resolve_fixed_action(fast_tool):
                fast_tool = None
        self._local.timings["route_ms"] = round((time.monotonic() - route_started) * 1000)
        log("Harness", "route", session=session_id, route=fast_tool or "vlm", policy="selective")
        available_tools = [t for t in HARNESS_TOOLS if t["function"]["name"] in permitted]
        # 本次 run 内每个只读调用的执行次数与文件版本；见 guard_repeated_call。
        repeat_calls = {}
        repeated_notices = set()
        # 循环不设固定步数：在"没有工具调用、也没有 steering
        # 消息"时自然结束，靠的是模型不再要工具，而不是数到第几轮。这里照同一
        # 原则，max_turns 退化为可选安全阀（0 = 不限），用来兜住坏提示词下
        # 真会空转整夜的情况。
        hard_limit = int(cfg["runtime"].get("max_turns", self.max_turns) or 0)
        for turn in itertools.count():
            if hard_limit and turn >= hard_limit:
                break
            if self._cancelled():
                return self._abort_reply(session_id)
            apply_pending_steerings()
            if on_status_change:
                on_status_change(f"处理第{turn + 1}步")
            def assemble():
                # 尾部每轮必然重新 prefill，体积必须受控。历史上这里直接
                # `{**task_state}` 展开，把 file_changes[].diff（整份文件）也带了进去，
                # 实测尾部膨胀到 12.8k 字符 ≈ 6.3k tokens，每轮净多 6–9 秒 TTFT。
                pinned = compact_checkpoint(task_state)
                # 冻结内容（人设/工具/准则/技能/工作空间）已经整段固化在首条 system；
                # 记忆已经作为条目进历史；尾部只剩"这一轮真正的运行数据"：
                # 任务检查点 + 时钟，合起来两三百字符，且恒为最后一条消息。
                live_context = render_tail_context(
                    runtime_context,
                    pinned,
                    events=tail_context.strip(),
                    clock=tail_clock,
                )
                return compactor.align_and_assemble_messages(
                    system,
                    history,
                    live_context,
                    image_bytes=image,
                    input_image_urls=input_images,
                )

            messages = assemble()
            budget = cfg["vlm"]["context_window"] - cfg["vlm"]["max_tokens"]
            soft = min(budget, cfg["vlm"]["context_window"] * cfg["vlm"]["compaction_threshold_ratio"])
            estimate = compactor.estimate_tokens(messages, tools)
            if estimate >= soft:
                # 软阈值：后台压。本轮照常用当前历史发请求——压缩是为了后面几轮，
                # 不是为了这一轮，所以绝不能在这里等一次摘要 LLM 调用。
                # 重复调用由 request_background 内部的在途标记和失败退避挡掉。
                compactor.request_background(history, reason="soft")
            if estimate > budget:
                # 硬上限：不压就发不出去，只能同步压一次。接受这一轮缓存被打穿，
                # 之后 begin_turn() 重新快照，让新摘要立刻生效。
                log("Harness", "context.hard_compact", session=session_id, step=turn + 1,
                    estimate=estimate, budget=budget)
                compactor._run_compaction_worker(history)
                compactor.begin_turn()
                messages = assemble()
                messages = compactor.trim_old_outputs(messages)
                estimate = compactor.estimate_tokens(messages, tools)
            if estimate > budget:
                log("Harness", "context.overflow", session=session_id, step=turn + 1,
                    estimate=estimate, budget=budget,
                    system_chars=len(system), history_chars=sum(len(str(m.get("content"))) for m in messages),
                    messages=len(messages))
                reply = "上下文超过当前模型配置的容量；原始历史已保留。请提高正确的context_window配置，或缩小本次输入。"
                append({"role": "assistant", "content": reply})
                return reply
            image = None
            is_fast = fast_tool is not None
            log(
                "Harness",
                "fast_tool.start" if is_fast else "model.start",
                session=session_id,
                step=turn + 1,
                messages=len(messages),
                tools=len(tools),
                # 冻结前缀指纹：跨轮应当恒定。一旦变化，说明有内容偷偷挤进了
                # system/tools，整段前缀缓存会被打穿，看这一个字段就能定位。
                system_fp=system_fp,
                tail_chars=len(str(messages[-1].get("content"))) if messages else 0,
            )
            if is_fast:
                def selected_call():
                    if False:
                        yield
                    fixed = resolve_fixed_action(fast_tool)
                    return "", [{"id": "fast_" + __import__("uuid").uuid4().hex, "type": "function",
                                 "function": {"name": fixed['tool'], "arguments": json.dumps(fixed['arguments'], ensure_ascii=False)}}], {}
                generator = selected_call()
            else:
                cancel_event = getattr(self._local, "cancel", None)

                def open_stream():
                    """兼容不接受 cancel_event 的 Brain 实现，优先带上取消信号。"""
                    attempts = (
                        {"model_override": model_override, "cancel_event": cancel_event},
                        {"model_override": model_override},
                        {},
                    )
                    last_error = None
                    for extra in attempts:
                        try:
                            return self.brain.stream_chat_and_tools(
                                messages, tools=tools or None, **extra
                            )
                        except TypeError as err:
                            last_error = err
                            if "unexpected keyword" not in str(err) and "argument" not in str(err):
                                raise
                    raise last_error

                generator = open_stream()
            text = []
            reasoning_chunks = []
            reasoning_done_flag = False
            calls = []
            usage = {}
            stream_error = False
            model_started = time.monotonic()
            first_text_ms = None
            streamed_live = False
            pending_text = []
            last_flush = 0.0

            def flush_text(force=False):
                """把累积的文本增量实时推给界面：50ms 节流，但收尾时强制冲刷，绝不丢尾字。"""
                nonlocal last_flush, streamed_live
                if not pending_text:
                    return
                if not on_text_chunk:
                    pending_text.clear()
                    return
                now = time.monotonic()
                if not force and now - last_flush < 0.05:
                    return
                chunk = "".join(pending_text)
                pending_text.clear()
                last_flush = now
                try:
                    on_text_chunk(chunk, "".join(text))
                    streamed_live = True
                except Exception as exc:
                    log("Harness", "stream.text_callback_error", session=session_id, error=str(exc)[:200])

            try:
                while True:
                    interrupt_evt = self._interrupt_stream.get(session_id)
                    if interrupt_evt and interrupt_evt.is_set():
                        generator.close()
                        interrupt_evt.clear()
                        log("Harness", "stream.interrupted_for_steering", session=session_id)
                        # An interrupted candidate is not an assistant delivery.
                        flush_text(force=True)
                        stream_reset("已被新的方向修正打断，正在按新指令重新生成")
                        apply_pending_steerings()
                        break
                    kind, data = next(generator)
                    if self._cancelled():
                        try:
                            generator.close()
                        except Exception:
                            pass
                        return self._abort_reply(session_id)
                    if kind == "reasoning_delta":
                        reasoning_chunks.append(data)
                        if on_reasoning_chunk:
                            on_reasoning_chunk(data, "".join(reasoning_chunks))
                    elif kind == "reasoning_complete":
                        if not reasoning_done_flag:
                            reasoning_done_flag = True
                            if on_reasoning_complete:
                                on_reasoning_complete("".join(reasoning_chunks))
                    elif kind == "text_delta":
                        if reasoning_chunks and not reasoning_done_flag:
                            reasoning_done_flag = True
                            if on_reasoning_complete:
                                on_reasoning_complete("".join(reasoning_chunks))
                        if first_text_ms is None:
                            first_text_ms = round((time.monotonic() - model_started) * 1000)
                        log("Harness", "text.delta", session=session_id, text=data)
                        text.append(data)
                        # Stream live so the web UI never looks frozen during long generations.
                        # A draft may still be rewritten after completion review; the final
                        # reply is reconciled by the caller, and discarded drafts are marked
                        # through on_text_reset.
                        pending_text.append(data)
                        flush_text()
                    elif kind == "tool_call_delta":
                        flush_text(force=True)
                        if reasoning_chunks and not reasoning_done_flag:
                            reasoning_done_flag = True
                            if on_reasoning_complete:
                                on_reasoning_complete("".join(reasoning_chunks))
                        if on_tool_delta:
                            try:
                                from core.streaming_parser import parse_tool_delta
                                parsed = parse_tool_delta(data.get("name", ""), data.get("arguments_so_far", ""), scope=scope)
                                enriched = {**data, **parsed}
                            except Exception:
                                enriched = data
                            on_tool_delta(enriched)
                    elif kind == "error":
                        flush_text(force=True)
                        stream_error = True
                        log("Harness", "model.error", session=session_id, step=turn + 1, detail=data)
            except StopIteration as done:
                if done.value:
                    _, calls, usage = done.value
            except Exception as exc:
                stream_error = True
                log("Harness", "model.error", session=session_id, step=turn + 1,
                    exception_type=type(exc).__name__, detail=str(exc)[:500])
                text.append("模型连接失败：" + str(exc)[:200])
                pending_text.append(text[-1])
            flush_text(force=True)
            pending_text.clear()
            # 模型流因用户停止而提前结束：不要走核验/重试/落库，也不要冒充正常回复。
            if self._cancelled():
                return self._abort_reply(session_id)
            fast_tool = None
            model_elapsed_ms = round((time.monotonic() - model_started) * 1000)
            if not is_fast:
                self._local.timings["model_ms"] += model_elapsed_ms
                self._local.timings["model_calls"] += 1
            log(
                "Harness",
                "fast_tool.end" if is_fast else "model.end",
                session=session_id,
                error=stream_error,
                elapsed_ms=model_elapsed_ms,
                first_text_ms=first_text_ms,
                tool_calls=len(calls),
                usage=usage,
            )
            if stream_error:
                error_text = "".join(text).strip()
                reply = error_text or "模型连接失败，请稍后重试。"
                if connection_retries < 1:
                    connection_retries += 1
                    log("Harness", "connection.retry", session=session_id, step=turn + 1, reason="模型连接或流中断，直接重试一次；不压缩上下文")
                    stream_reset("模型连接中断，正在重试")
                    if on_status_change:
                        try:
                            on_status_change("模型连接中断，正在直接重试...")
                        except Exception:
                            pass
                    time.sleep(1)
                    continue
                append({"role": "assistant", "content": reply})
                return reply
            else:
                connection_retries = 0
            if not calls:
                candidate_reply = "".join(text)
                media_refs = re.findall(r"media://((?:img|vid|aud)_[A-Za-z0-9]+)", candidate_reply)
                try:
                    if media_refs:
                        from core.media_store import MediaStore
                        store = MediaStore()
                        for ref in media_refs:
                            store.get(ref, scope)
                except (ValueError, OSError) as exc:
                    log("Harness", "completion.invalid_media", session=session_id, error=str(exc))
                    if review_retries < int(cfg["runtime"].get("completion_retries", 1)):
                        review_retries += 1
                        tools = available_tools
                        stream_reset("媒体引用未通过校验，正在重新处理")
                        append_notice("上一模型回复中的媒体引用未通过实际缓存校验，尚未交付。不得编造media_ref或声称已经生成。请依据当前媒体引用和可用技能（见工具目录）重新处理，或调用register_media登记真实文件；可先list_media。没有权限或生成失败时明确说明，不要继续给无效媒体链接。")
                        continue
                    reply = "媒体处理未完成：返回的媒体引用不可用，没有可发送的结果。请重试或重新上传。"
                    append({"role": "assistant", "content": reply})
                    return reply
            message = {"role": "assistant", "content": "".join(text) or None}
            if reasoning_chunks:
                message["reasoning_content"] = "".join(reasoning_chunks)
            if calls:
                # 模型边说边做时照常显示这段说明文字，不再额外问一次"这是不是提前
                # 宣称完成"：那会丢掉正常的过程说明，也拖慢每一个中间步骤。
                message["tool_calls"] = calls
                append(message)
            if calls and on_intermediate and "".join(text).strip():
                try:
                    progress = (message.get('content') or '').strip()
                    on_intermediate(progress)
                except Exception as exc:
                    log("Harness", "intermediate.error", session=session_id, error=str(exc))
            if not calls:
                action, final_reply = finish_turn("".join(text) or "模型没有返回有效回答。")
                if action == "continue":
                    continue
                reply = final_reply
                message["content"] = reply
                append(message)
                if on_text_chunk and (not streamed_live or reply != "".join(text)):
                    # 已经流式推送过同一段文本就不重复补发；若最终文案被核验改写过，
                    # 仍要把定稿推给界面（前端以 full 覆盖当前段，不会拼接重复）。
                    on_text_chunk(reply, reply)
                if all(t["result"].get("ok") for t in trace) and trace:
                    # Fast local synthesis from observed successful tool steps, no extra cloud round trip.
                    if self.on_completed:
                        self.on_completed(user_prompt, trace)
                    else:
                        self.skill_manager.learn_from_trace(user_prompt, trace)
                return reply
            for call in calls:
                if self._cancelled():
                    # 工具批次执行中按停止：立刻中止，未执行的部分不再补跑。
                    return self._abort_reply(session_id)
                tool_started = time.monotonic()
                name = call.get("function", {}).get("name", "")
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("参数必须是JSON对象")
                    if on_status_change:
                        try:
                            on_status_change(f"正在执行工具：{name}")
                        except Exception:
                            pass
                    if on_tool_call:
                        try:
                            on_tool_call(name, args)
                        except Exception:
                            pass
                    signature = call_signature(name, args)
                    blocked, notice = before_tool_call(name, args, signature)
                    if blocked is not None:
                        # 结果完全一样，再执行一次只是重复烧上下文；直接回退提示。
                        log("Harness", "tool.duplicate_blocked", session=session_id,
                            tool=name, signature=signature[:200], step=turn + 1)
                        result = blocked
                        if on_status_change:
                            try:
                                on_status_change(f"跳过重复调用：{name}")
                            except Exception:
                                pass
                    else:
                        result = self._dispatch_tool(
                            name, args, scope, session_id, channel, target, permitted, task_state=task_state
                        )
                        if notice:
                            result = {**result, "notice": notice}
                    tool_images = result.pop("_images", None)
                    tool_image = result.pop("_image", None)
                    if name in ("capture_screen", "get_media") and result.get("ok"):
                        image = tool_images or tool_image
                    elif name == "execute_skill" and result.get("ok"):
                        # Workflow screenshots are returned internally, never JSON-encoded as bytes.
                        image = tool_images or tool_image or image

                    for k in list(result.keys()):
                        if k.startswith("_"):
                            result.pop(k, None)

                    clean_tool_result = dict(result)
                    if on_tool_result:
                        try:
                            on_tool_result(name, clean_tool_result)
                        except Exception:
                            pass
                except Exception as exc:
                    args = {}
                    result = {"ok": False, "error": str(exc)[:500]}
                    clean_tool_result = dict(result)
                self._local.timings["tool_ms"] += round((time.monotonic() - tool_started) * 1000)
                record = repeat_calls.get(signature)
                if record is not None and result.get("ok") and name == "read_file":
                    # 记下"读到哪了"，重复被拦时才能给出具体续读位置而不是空劝。
                    record["last"] = {"next_start": result.get("next_start_line")}
                if not result.get("ok") and not tools:
                    # A model can emit a historical tool call even with tools omitted.
                    # Let it repair the call using only this source's current permissions.
                    tools = available_tools
                trace.append(
                    {
                        "tool": name,
                        "arguments": args,
                        "result": {
                            k: v for k, v in result.items() if not k.startswith("_")
                        },
                    }
                )
                task_state.setdefault("last_tools", []).append({"call_id": call["id"], "tool": name, "ok": bool(result.get("ok")),
                    "target": str(args.get("file_path") or args.get("skill_name") or args.get("command", ""))[:400],
                    "error": str(result.get("error", ""))[:400], "result_preview": str(result.get("output", ""))[:600]})
                task_state["last_tools"] = task_state["last_tools"][-64:]
                task_state["executed_steps"] = task_state.get("executed_steps", 0) + 1
                if result.get("ok"):
                    artifacts = task_state.setdefault("artifacts", [])
                    if result.get("path") and result["path"] not in artifacts:
                        artifacts.append(result["path"])
                    if result.get("gif_path") and result["gif_path"] not in artifacts:
                        artifacts.append(result["gif_path"])
                    for sp in result.get("screenshot_paths", []):
                        if sp not in artifacts:
                            artifacts.append(sp)
                    if result.get("file_changed"):
                        file_changes = task_state.setdefault("file_changes", [])
                        # 只记"改了哪个文件、几行"。整份 diff 会随 task_state 被写进
                        # 每轮尾巴的 <task_checkpoint>，那是每轮都要重新 prefill 的
                        # 地方，一份 10KB 的 diff 等于每轮白烧 4–5 秒。
                        file_changes.append({
                            "path": result.get("rel_path") or result.get("path"),
                            "added": result.get("added", 0),
                            "removed": result.get("removed", 0),
                        })
                    for asset in result.get("assets", []) if isinstance(result.get("assets", []), list) else []:
                        ref = asset.get("media_ref") if isinstance(asset, dict) else None
                        if ref and ref not in artifacts:
                            artifacts.append(ref)
                for changed_path in result.get("changed_files", []):
                    task_state.setdefault("file_changes", []).append({"path": changed_path})
                self.memory.save_task_state(session_id, task_state)
                output = json.dumps(
                    clean_tool_result,
                    ensure_ascii=False,
                    default=lambda o: f"<bytes {len(o)}>" if isinstance(o, bytes) else str(o),
                )
                cap = int(cfg["runtime"]["max_output_chars"])
                if len(output) > cap:
                    output = json.dumps(
                        {
                            "ok": clean_tool_result.get("ok", False),
                            "output": output[:cap],
                            "truncated": True,
                        },
                        ensure_ascii=False,
                        default=lambda o: f"<bytes {len(o)}>" if isinstance(o, bytes) else str(o),
                    )
                append({"role": "tool", "tool_call_id": call["id"], "content": output})
                if name in STABLE_READ_TOOLS:
                    record = repeat_calls.get(signature) or {}
                    if record.get("count", 0) >= DUPLICATE_ABORT_AT:
                        # 同一只读调用重复到第 6 次：提示已经给过三轮，再轮下去只是把
                        # max_turns 耗在空转上。如实收尾，把决定权交回用户。
                        log("Harness", "tool.duplicate_abort", session=session_id,
                            tool=name, signature=signature[:200], step=turn + 1)
                        reply = (f"我在同一个操作上空转了 {record['count']} 次：反复以相同参数调用 "
                                 f"{name}，结果始终没有变化，已停止本轮以免继续消耗时间。"
                                 f"可以告诉我具体需要文件/结果中的哪一部分，或换个方式处理。")
                        append({"role": "assistant", "content": reply})
                        task_state["status"] = "unverified"
                        self.memory.save_task_state(session_id, task_state)
                        return reply
                if result.get("ok") and name == "activate_skill":
                    # 技能目录已固化在 system 前缀中，本轮内不再重排；
                    # activate_skill 的返回结果本身已包含技能信息。
                    # 下一轮 run 会以新的稳定排序重建 system 前缀。
                    pass
            # 只读调用的提示已经写在工具结果里；这里再补一次系统级提醒，因为小模型
            # 常常读完工具输出就忘。每个签名只提醒一次，避免自己变成刷屏源。
            stalled = [
                f"{sig.split(':', 1)[0]} × {rec['count']}"
                for sig, rec in repeat_calls.items()
                if rec.get("count", 0) >= DUPLICATE_BLOCK_AT and sig not in repeated_notices
            ]
            if stalled:
                repeated_notices.update(
                    sig for sig, rec in repeat_calls.items()
                    if rec.get("count", 0) >= DUPLICATE_BLOCK_AT)
                append_notice("【重复调用提醒】以下调用以完全相同的参数重复执行了多次："
                       + "、".join(stalled)
                       + "。如果结果没有变化，重复同样的调用不会带来新信息：请换参数"
                         "（如 start_line）、换工具，或直接用已经拿到的结果继续下一步；"
                         "如果是在确认某处改动是否生效，请直接查看该结果本身而不是重跑。")
            compactor.update_usage(usage.get("prompt_tokens", 0), history)
        limit = hard_limit or "∞"
        checkpoint = {"original_goal": task_goal[:6000], "latest_request": user_prompt[:2000],
                      "last_tools": [{"tool": t["tool"], "ok": t["result"].get("ok"), "error": str(t["result"].get("error", ""))[:300]} for t in trace[-8:]],
                      "instructions": "本轮预算已用完，任务尚未确认完成。用户发来继续后依据已有工具结果补齐剩余任务，不重复已成功的有副作用操作；结果未知时先验证。新指令优先。"}
        append_notice("[任务续接检查点]\n" + json.dumps(checkpoint, ensure_ascii=False))
        task_state["status"] = "awaiting_input"
        self.memory.save_task_state(session_id, task_state)
        reply = f"已达到本次任务 {limit} 轮上限，进度和工具结果已保存。发送“继续”可接着执行，也可以补充或调整要求。"
        append({"role": "assistant", "content": reply})
        return reply

    def _dispatch_tool(
        self,
        name,
        args,
        scope="local",
        session_id="local:pet",
        channel="pet",
        target="",
        permitted=None,
        task_state=None,
        actor=None,
    ):
        status = getattr(self._local, "status", None)
        if status:
            label = {
                "execute_command": "正在运行命令",
                "execute_skill": "正在执行技能",
                "capture_screen": "正在查看屏幕/核验网页",
                "list_skills": "正在查询可用技能",
                "check_task": "正在查询后台任务",
                "wait_task": "正在等待后台任务",
                "cancel_task": "正在取消后台任务",
            }.get(name, "正在调用 " + name)
            status(label)
        started = time.monotonic()
        log("Harness", "tool.start", session=session_id, tool=name, arguments=args)
        try:
            before_sources = None
            if name in {"execute_command", "execute_skill"}:
                from core.verification import source_snapshot
                from core.workspace import workspace_for
                before_sources = source_snapshot(workspace_for(scope))
            result = self._execute_tool(
                name, args, scope, session_id, channel, target, permitted,
                task_state=task_state,
                actor=getattr(self._local, "actor", "") if actor is None else actor,
            )
            if before_sources is not None:
                after_sources = source_snapshot(workspace_for(scope))
                result["changed_files"] = [p for p in before_sources.keys() | after_sources.keys()
                                           if before_sources.get(p) != after_sources.get(p)]
            log(
                "Harness",
                "tool.end",
                session=session_id,
                tool=name,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                result=result,
            )
            return result
        except Exception as exc:
            log("Harness", "tool.error", session=session_id, tool=name, error=str(exc))
            raise

    def _execute_tool(
        self,
        name,
        args,
        scope="local",
        session_id="local:pet",
        channel="pet",
        target="",
        permitted=None,
        task_state=None,
        actor="",
    ):
        from core.config import load_config, save_config
        from core.process import run_bounded
        from core.skill_manager import validate_args

        if permitted is None:
            permitted = {t["function"]["name"] for t in HARNESS_TOOLS}
        if self._cancelled():
            return {"ok": False, "error": "任务已取消"}
        if name not in permitted:
            return {"ok": False, "error": "当前来源没有此工具权限"}
        from core.qq_authority import tool_access
        if getattr(self._local, "readonly", False) and name not in {"read_file", "capture_screen", "list_media", "get_media", "list_skills", "read_history", "read_chat_history"} and not (name == "manage_permissions" and args.get("action") == "list"):
            return {"ok": False, "error_code": "READ_ONLY", "retryable": False, "error": "本次任务只读，命令和技能都不能执行。改用 capture_screen 看屏幕、read_file 读文件、read_history 读历史来核实，并说明你实际看到的依据；看不到就说看不到，不要猜。"}
        is_web = (channel == "web" or session_id.startswith("web:"))
        if not is_web and not tool_access(scope, name, args, actor=actor):
            return {"ok": False, "error_code": "PERMISSION_DENIED", "tool": name, "retryable": False,
                    "skill_name": args.get("skill_name") if isinstance(args, dict) else None,
                    "error": "执行前权限校验拒绝，操作尚未执行。请向用户说明具体缺少的工具/技能权限，可请主人用自然语言授权；不要编造执行成功。"}
        spec = next(
            (
                t["function"]["parameters"]
                for t in HARNESS_TOOLS
                if t["function"]["name"] == name
            ),
            None,
        )
        if spec is None:
            return {"ok": False, "error": "未知工具"}
        try:
            if name == "execute_skill" and isinstance(args, dict) and isinstance(args.get("arguments"), str):
                try:
                    values = json.loads(args["arguments"])
                except ValueError:
                    raise ValueError("参数 arguments 必须是JSON对象，收到的字符串不是有效JSON")
                if not isinstance(values, dict):
                    raise ValueError("参数 arguments 必须是JSON对象")
                args = {**args, "arguments": values}
                log("Harness", "tool.arguments_normalized", session=session_id, tool=name,
                    field="arguments", from_type="string", to_type="object")
            validate_args(spec, args)
            if name == "read_chat_history":
                from core.media_store import media_scope
                rows, summary = self.memory.chat_snapshot(media_scope(scope), limit=min(args.get("limit", 20), 50), before_id=args.get("before_id"))
                return {"ok": True, "messages": rows, "summary": summary}
            if name == "manage_permissions":
                from core.qq_authority import manage_permissions
                return manage_permissions(scope, args, {t["function"]["name"] for t in HARNESS_TOOLS}, actor=actor)
            if name == "restart_bot":
                # 重启只受理，不立刻杀进程：回复要先发出去，发完再优雅停机。
                handler = getattr(self, "on_restart", None)
                if not callable(handler):
                    return {"ok": False, "error": "当前运行环境没有接入重启通道，无法重启"}
                reason = str(args.get("reason") or "")[:200]
                if not handler(reason, session_id):
                    return {"ok": False, "error": "重启请求已在处理中，无需重复发起"}
                return {"ok": True, "restarting": True, "reason": reason,
                        "message": "已受理重启请求：本条回复发出后进程会优雅停机，启动器会重新拉起 MiniBot。"}
            from core.workspace import resolve_file, workspace_for
            if name in ("list_media", "get_media", "register_media"):
                from core.media_store import MAX_BYTES, MediaStore
                store = MediaStore()
                if name == "list_media":
                    return {"ok": True, "media": store.list(scope, args.get("limit", 12), args.get("message_id", ""))}
                if name == "get_media":
                    manifest, _ = store.get(args["media_ref"], scope)
                    result = {"ok": True, "kind": store.kind_of(manifest), **manifest}
                    if manifest["mime"].startswith("image/"):
                        result["_image"] = store.preview(args["media_ref"], scope)
                    return result
                path = resolve_file(args["file_path"], scope)
                if not path.is_file():
                    raise ValueError("media文件不存在或不是普通文件")
                if path.stat().st_size > MAX_BYTES["video"]:
                    raise ValueError("媒体文件超过100MB上限")
                asset = store.add(path.read_bytes(), scope, origin="registered", label=path.name)
                return {"ok": True, "kind": store.kind_of(asset), **asset}
            if name == "execute_command":
                command = args["command"]
                assessment = assess_command(
                    command, is_tainted=self.is_tainted(session_id)
                )
                if not assessment["allowed"]:
                    return {"ok": False, "error": assessment["reason"]}
                cmd = (
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        "$ProgressPreference='SilentlyContinue';"
                        "$ErrorActionPreference='Continue';"
                        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();$OutputEncoding=[Console]::OutputEncoding;"
                        "[System.Net.WebRequest]::DefaultWebProxy=[System.Net.WebProxy]::new();"
                        + command + "; if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
                    ]
                    if os.name == "nt"
                    else command
                )
                from core.task_manager import TaskManager
                timeout_val = args.get("timeout_sec", 15)
                background = args.get("background", False)
                tm = TaskManager.get_instance()
                task = tm.start_task(
                    cmd,
                    name=f"cmd:{command[:40]}",
                    cwd=str(workspace_for(scope)),
                    shell=os.name != "nt",
                    metadata={"command": command, "scope": scope},
                )
                if background:
                    return {
                        "ok": True,
                        "status": "running",
                        "task_id": task.task_id,
                        "output": f"命令已在后台启动运行。\n任务ID: {task.task_id}\n你可以调用 check_task 或 wait_task 查询状态与结果。",
                    }
                cancel_event = getattr(self._local, "cancel", None)
                if cancel_event:
                    def _watch_cancel():
                        cancel_event.wait()
                        task.cancel()
                    import threading
                    threading.Thread(target=_watch_cancel, daemon=True).start()
                return tm.wait_task(task.task_id, timeout_sec=timeout_val)
            if name == "read_file":
                fp_lower = str(args.get("file_path", "")).lower()
                sensitive_patterns = (
                    "config.json",
                    "config.local.json",
                    ".env",
                    "id_rsa",
                    ".ssh",
                    "credentials",
                    "password",
                    "secret",
                    "memory.db",
                )
                if any(pat in fp_lower for pat in sensitive_patterns):
                    self.mark_tainted(session_id)
                start = max(1, args.get("start_line", 1))
                count = max(1, min(args.get("max_lines", 120), 500))
                rows = []
                size = 0
                eof = False
                with resolve_file(args["file_path"], scope).open(
                    "r", encoding="utf-8", errors="replace"
                ) as stream:
                    # Bounded readline also handles gigantic single-line log files.
                    line_no = 0
                    while line_no < start - 1:
                        part = stream.readline(8192)
                        if not part:
                            break
                        if part.endswith("\n"):
                            line_no += 1
                    while len(rows) < count and size < 12000:
                        part = stream.readline(min(8192, 12000 - size))
                        if not part:
                            eof = True
                            break
                        rows.append(part)
                        size += len(part)
                # 明确告诉模型"后面还有多少"：以前读到窗口末尾时不带任何续读线索，
                # 模型只能原样再读一遍，于是出现同一个文件连续读七次。
                return {
                    "ok": True,
                    "output": "".join(rows),
                    "start_line": start,
                    "next_start_line": start + len(rows),
                    "has_more": not eof,
                    "truncated": size >= 12000,
                }
            if name == "write_file":
                p = resolve_file(args["file_path"], scope)
                p.parent.mkdir(parents=True, exist_ok=True)
                before_content = p.read_text(encoding="utf-8") if p.exists() else None
                p.write_text(args["content"], encoding="utf-8")
                from core.checkpoint import get_checkpoint_manager
                change = get_checkpoint_manager().record_file_change(
                    session_id=session_id,
                    scope=scope,
                    abs_path=str(p),
                    before_content=before_content,
                    after_content=args["content"],
                )
                return {
                    "ok": True,
                    "path": str(p),
                    "rel_path": change["rel_path"],
                    "added": change["added"],
                    "removed": change["removed"],
                    "file_changed": True,
                    # 全文就在本次调用的 content 参数里，不回传 diff。
                    "diff": "",
                }
            if name == "edit_file":
                p = resolve_file(args["file_path"], scope)
                if not p.exists():
                    return {"ok": False, "error": f"文件不存在: {args['file_path']}"}
                before_content = p.read_text(encoding="utf-8")
                target = args["target"]
                replacement = args["replacement"]
                if target not in before_content:
                    return {"ok": False, "error": "文件中未找到精确匹配的目标文本 target，请先调用 read_file 查看文件内容"}
                after_content = before_content.replace(target, replacement, 1)
                p.write_text(after_content, encoding="utf-8")
                from core.checkpoint import get_checkpoint_manager
                change = get_checkpoint_manager().record_file_change(
                    session_id=session_id,
                    scope=scope,
                    abs_path=str(p),
                    before_content=before_content,
                    after_content=after_content,
                )
                return {
                    "ok": True,
                    "path": str(p),
                    "rel_path": change["rel_path"],
                    "added": change["added"],
                    "removed": change["removed"],
                    "file_changed": True,
                    # 只给 hunk 头（改了哪几处、在第几行），不复述被改的内容——
                    # target / replacement 已经在本次调用的参数里。
                    "diff": diff_for_model(change["diff"], False),
                }
            if name == "capture_screen":
                from core.browser_verifier import capture_browser_or_screen
                file_path = args.get("file_path")
                url = args.get("url")

                # If file_path/url not specified, try to auto-detect recently modified HTML/SVG from task_state
                if not file_path and not url and task_state:
                    for fc in reversed(task_state.get("file_changes", [])):
                        p = fc.get("path") or ""
                        if p.lower().endswith((".html", ".htm", ".svg")):
                            file_path = p
                            break
                    if not file_path:
                        for art in reversed(task_state.get("artifacts", [])):
                            if isinstance(art, str) and art.lower().endswith((".html", ".htm", ".svg")):
                                file_path = art
                                break

                return capture_browser_or_screen(
                    file_path=file_path,
                    url=url,
                    frames=args.get("frames", 1),
                    interval_seconds=args.get("interval_seconds", 0.5),
                    reason=args.get("reason", ""),
                    scope=scope,
                    session_id=session_id,
                    fallback_desktop_capture=self.perception.capture_screen_jpeg if hasattr(self, "perception") else None,
                    width=args.get("width", 1280),
                    height=args.get("height", 800),
                )
            if name == "execute_skill":
                if args["skill_name"] == "qq_games":
                    from core.qq_games import play
                    from core.default_skills import REGISTRY
                    from core.skill_manager import validate_args
                    values = validate_args(REGISTRY["qq_games"]["parameters"],args.get("arguments",{}))
                    return play(values, scope="qq:games:group:"+target if channel=="qq_group" else session_id, memory=self.memory)
                nested_images = []

                def dispatch(tool, arguments):
                    result = self._dispatch_tool(
                        tool, arguments, scope, session_id, channel, target, permitted, actor=actor
                    )
                    if result.get("_image"):
                        nested_images.append(result.pop("_image"))
                    return result

                result = self.skill_manager.execute_skill(
                    args["skill_name"],
                    args.get("arguments", {}),
                    dispatch,
                    cancel_event=getattr(self._local, "cancel", None),
                    media_scope=scope,
                    timeout=args.get("timeout_sec"),
                    background=args.get("background", False),
                )
                if nested_images:
                    result["_image"] = nested_images[-1]
                return result
            if name == "list_skills":
                query = args.get("query", "")
                skills = self.skill_manager.describe(query)
                return {
                    "ok": True,
                    "skills": skills,
                    "total": len(skills),
                    "unavailable": self.skill_manager.unavailable_skills(),
                }
            if name == "check_task":
                from core.task_manager import TaskManager
                return TaskManager.get_instance().check_task(args["task_id"])
            if name == "wait_task":
                from core.task_manager import TaskManager
                return TaskManager.get_instance().wait_task(args["task_id"], timeout_sec=args.get("timeout_sec", 15))
            if name == "cancel_task":
                from core.task_manager import TaskManager
                return TaskManager.get_instance().cancel_task(args["task_id"])
            if name == "activate_skill":
                self.skill_manager.reload_skills()
                return {"ok": True, "skills": self.skill_manager.describe("", limit=50, stable=True),
                        "output": "技能目录已重新扫描加载"}
            if name == "schedule_reminder":
                condition = args.get("condition", "")
                if condition and not tool_access(scope, "capture_screen", actor=actor):
                    raise ValueError("当前来源无权创建本机状态条件")
                if condition not in (
                    "",
                    "idle",
                    "active",
                    "error",
                    "error_cleared",
                ) and not condition.startswith("window:"):
                    raise ValueError("不支持的条件语法")
                import hashlib

                dedupe = hashlib.sha256(
                    json.dumps([scope, session_id, args], sort_keys=True).encode()
                ).hexdigest()
                task = self.memory.add_delayed_intent(
                    args["trigger_at"],
                    args["message"],
                    condition,
                    scope,
                    session_id,
                    channel,
                    target,
                    dedupe,
                )
                return {"ok": True, "task_id": task}
            if name == "read_history":
                return {
                    "ok": True,
                    "messages": self.memory.read_archived_history(
                        session_id, args.get("after_id", 0), args.get("limit", 10)
                    ),
                }
            if name == "list_reminders":
                return {
                    "ok": True,
                    "tasks": self.memory.list_intents(scope, session_id),
                }
            if name == "update_reminder":
                if not args.get("cancel") and "trigger_at" not in args:
                    raise ValueError("请提供改期时间")
                ok = self.memory.update_intent(
                    args["task_id"],
                    scope,
                    session_id,
                    args.get("trigger_at"),
                    args.get("cancel", False),
                )
                return {"ok": ok}
            if name == "remember_fact":
                self.memory.set_fact(
                    args["key"], args["value"], scope, source="user_explicit"
                )
                return {"ok": True}
            if name == "forget_fact":
                self.memory.delete_fact(args["key"], scope)
                return {"ok": True}
            if name == "notification_feedback":
                value = args["value"]
                self.memory.feedback(scope, "notification", value)
                if value in ("mute", "resume") and scope == "local":
                    save_config({"proactive": {"muted": value == "mute"}})
                return {"ok": True}
            if name == "undo_memory":
                return self.memory.undo_last_fact_change(scope=scope)
            if name == "manage_goal":
                action = args.get("action")
                mgr = self.memory.goals
                if action == "create":
                    title = args.get("title")
                    if not title:
                        return {"ok": False, "error": "创建目标需要提供 title"}
                    goal = mgr.create_goal(
                        title=title,
                        category=args.get("category", "project"),
                        steps=args.get("steps", []),
                        cadence=args.get("cadence", "daily 09:00"),
                        target_date=args.get("target_date", ""),
                        scope=scope,
                    )
                    return {
                        "ok": True,
                        "goal": goal,
                        "output": f"成功创建目标《{title}》[{goal['id']}]，共 {len(goal['steps'])} 个步骤",
                    }
                if action == "list":
                    goals = mgr.list_goals(
                        status=args.get("status", "active"), scope=scope
                    )
                    return {"ok": True, "count": len(goals), "goals": goals}
                if action == "get":
                    goal_id = args.get("goal_id")
                    if not goal_id:
                        return {"ok": False, "error": "需要提供 goal_id"}
                    g = mgr.get_goal(goal_id)
                    return {"ok": bool(g), "goal": g}
                if action == "update_step":
                    goal_id = args.get("goal_id")
                    step_id = args.get("step_id")
                    status = args.get("step_status")
                    if not goal_id or step_id is None or not status:
                        return {
                            "ok": False,
                            "error": "update_step 需要 goal_id, step_id, step_status",
                        }
                    return mgr.update_step(
                        goal_id=goal_id,
                        step_id=int(step_id),
                        status=status,
                        evidence=args.get("evidence", ""),
                    )
                if action == "set_status":
                    goal_id = args.get("goal_id")
                    status = args.get("status") or args.get("step_status")
                    if not goal_id or not status:
                        return {
                            "ok": False,
                            "error": "set_status 需要 goal_id 和 status",
                        }
                    return mgr.set_status(goal_id, status)
                if action == "propose":
                    goal_id = args.get("goal_id")
                    new_steps = args.get("steps", [])
                    return mgr.propose_revision(
                        goal_id, new_steps, reason=args.get("evidence", "")
                    )
                return {"ok": False, "error": f"不支持的目标操作: {action}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}
        return {"ok": False, "error": "工具未实现"}
