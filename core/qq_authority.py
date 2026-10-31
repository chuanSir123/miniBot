"""Owner tool authority comes from the current sender ID, never quoted content.

权限判断的唯一依据是「当前这条消息的发送者 QQ 号」（actor）+ 配置里的授权表。
模型（VLM）和决策模型（Jev）只负责理解意图、挑动作，不参与授权判定：
它们看到的 permissions 只是程序判定结果的转述，即使被引用文本或昵称带偏，
真正执行时仍会在 _execute_tool 里按 actor 重新判定一次。

pet / web / 本地工作空间不是 QQ 会话，直接放行，不做权限判断。
"""
import re
import time
from core.config import load_config, save_config

BASE_TOOLS = {"read_history", "schedule_reminder", "list_reminders", "list_skills", "list_media", "get_media",
              "update_reminder", "remember_fact", "forget_fact", "notification_feedback", "undo_memory", "manage_goal", "read_chat_history"}

# 本地/桌宠/Web 的受信前缀；其余形如自定义工作空间名称的作用域同样受信。
TRUSTED_PREFIXES = ("local:", "main:", "web:", "pet:")
TRUSTED_SCOPES = {"", "main", "local", "pet"}
# 只有这些前缀代表"QQ 侧的真实用户消息"，必须按 QQ 号判权限。
QQ_SCOPE_PREFIXES = ("qq:", "qq_group_", "qq_private_", "user:", "group:")

IMPLICIT_OWNER, IMPLICIT_MEMBER = "implicit_owner", "implicit_member"

# 群成员职位。owner=群主，admin=管理员，member=普通成员。
# 职位来自 NapCat 的 get_group_member_info（核验）或事件里的 sender.role（仅声明）。
ROLES = ("owner", "admin", "member")
ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "成员"}
_ROLE_SUFFIX = re.compile(r"@(owner|admin|member)$")


def group_role_verify():
    """是否用接口核验职位。核验只影响"展示得准不准"，不影响任何权限。"""
    return bool(load_config()["channels"]["napcat"].get("group_role_verify", True))


def role_of(actor):
    """从 actor 字符串尾部取职位；只有核验过的职位才会被写进 actor。"""
    match = _ROLE_SUFFIX.search(str(actor or ""))
    return match[1] if match else ""


def role_from_event(event):
    """返回 (职位, 是否核验过)。

    ``event["_role"]`` 由 channel 层调 NapCat 接口核验后写入；没有它时退回
    ``sender.role``（NapCat/OneBot 群消息自带），但那只是发送方声明，**不用于授权**，
    只用于向模型展示"这条是谁、什么职位"。
    """
    if not isinstance(event, dict):
        return "", False
    info = event.get("_role")
    if isinstance(info, dict) and info.get("role") in ROLES:
        return str(info["role"]), bool(info.get("verified"))
    declared = str((event.get("sender") or {}).get("role") or "").lower()
    return (declared, False) if declared in ROLES else ("", False)


def owner_ids():
    return {str(x) for x in load_config()["channels"]["napcat"]["owner_ids"]}


def owner_tools_enabled():
    return bool(load_config()["channels"]["napcat"].get("allow_owner_tools", True))


def actor_from_event(event):
    """把一条 QQ 消息折算成权限身份，只取发送者 QQ 号。

    群聊用 ``qq:group:<群号>:user:<QQ>``，私聊用 ``qq:private:<QQ>``；
    事件里拿不到发送者 QQ 时返回空串，调用方会据此拒绝执行。
    群聊且职位经接口核验过时，尾部追加 ``@owner/@admin/@member``：
    授权用职位依赖这个后缀，未核验的声明绝不进 actor。
    """
    if not event:
        return ""
    uid = str(event.get("user_id") or "").strip()
    if not uid.isdigit():
        return ""
    if event.get("message_type") == "group":
        gid = str(event.get("group_id") or "").strip()
        if not gid.isdigit():
            return ""
        role, verified = role_from_event(event)
        return f"qq:group:{gid}:user:{uid}" + (f"@{role}" if verified and role in ROLES else "")
    return f"qq:private:{uid}"


def is_trusted_scope(scope):
    """本地、桌宠、Web 与自定义工作空间不参与 QQ 权限判断。"""
    value = (scope or "").strip()
    if value in TRUSTED_SCOPES or value.startswith(TRUSTED_PREFIXES):
        return True
    return not value.startswith(QQ_SCOPE_PREFIXES)


def scope_identity(scope):
    # actor 可能带职位后缀（qq:group:88:user:456@admin），解析身份前先剥掉。
    scope = _ROLE_SUFFIX.sub("", (scope or "").strip())
    private = re.fullmatch(r"qq:private:(\d+)", scope) or re.fullmatch(r"qq_private_(\d+)", scope)
    group = re.fullmatch(r"qq:group:(\d+):user:(\d+)", scope) or re.fullmatch(r"qq_group_(\d+):user:(\d+)", scope)
    if private:
        return private[1], "private"
    if group:
        return group[2], "group:" + group[1]
    m_grp = re.fullmatch(r"qq_group_(\d+)", scope)
    if m_grp:
        return "", "group:" + m_grp[1]
    return "", ""


def _authority(scope, actor=""):
    """解析出 (发送者QQ, 会话范围, 是否QQ会话)。actor 优先，绝不从消息内容推断。"""
    if is_trusted_scope(scope):
        return "", "", False
    uid, chat = scope_identity(actor or scope)
    return uid, chat, True


def is_owner_scope(scope, actor=""):
    uid, _, qq_session = _authority(scope, actor)
    if not qq_session:
        return True
    return bool(uid) and uid in owner_ids()


def matching_grants(scope, actor=""):
    uid, chat, qq_session = _authority(scope, actor)
    if not qq_session:
        return []
    now = time.time()
    return [r for r in load_config()["channels"]["napcat"].get("permission_grants", [])
            if r["user_id"] in (uid, "*") and r["scope"] in ("all", chat) and (not r.get("expires_at") or r["expires_at"] > now)]


def _directive(scope, kind, name, actor=""):
    uid, chat, _ = _authority(scope, actor)
    rows = [r for r in matching_grants(scope, actor) if r["kind"] == kind and r["name"] in ("*", name)]
    if not rows:
        return None
    row = max(rows, key=lambda r: (r["scope"] == chat, r["user_id"] == uid, r["name"] == name, r.get("updated_at", 0)))
    return row["allow"]


def tool_access(scope, name, args=None, actor=""):
    """执行期权限判定。

    ``actor`` 是当前消息发送者的权限身份（``actor_from_event`` 的产物）。
    QQ 会话必须由它给出 QQ 号；拿不到就拒绝，绝不退化成"让模型自己看着办"。
    """
    args = args or {}
    uid, _, qq_session = _authority(scope, actor)
    if not qq_session:
        return True
    if not uid:
        return False
    if name == "manage_permissions":
        return is_owner_scope(scope, actor) or args.get("action") == "list"
    # 主人不做逐工具/逐技能授权判断，只受"远端工具总开关"约束。
    if is_owner_scope(scope, actor):
        return owner_tools_enabled()
    tool = _directive(scope, "tool", name, actor)
    skill = _directive(scope, "skill", args.get("skill_name", ""), actor) if name == "execute_skill" else None
    if tool is False or skill is False:
        # 显式撤销（allow=False）对任何身份都生效。
        return False
    if name in BASE_TOOLS or (name == "execute_skill" and args.get("skill_name") == "qq_games"):
        return True
    if not owner_tools_enabled():
        # 总开关关闭时，被授权人也不得执行宿主工具。
        return False
    # 注意：群主/管理员身份**不参与**授权。职位只用来让 bot 知道是谁在说话，
    # 工具与技能一律看主人配置里的授权表 + 总开关。
    return tool is True or skill is True


def permission_summary(scope, actor=""):
    """给模型/Jev 的权限转述；只是解释，不是判定入口。"""
    uid, _, qq_session = _authority(scope, actor)
    owner = is_owner_scope(scope, actor)
    enabled = owner_tools_enabled()
    return {
        "qq_session": qq_session,
        "actor_user_id": uid,
        "owner": owner,
        "full_tools": (not qq_session) or (owner and enabled) or scope_owner_tools_allowed(scope, actor),
        "owner_tools_enabled": enabled,
        "default_tools": sorted(BASE_TOOLS),
        "default_skills": ["qq_games"],
        "grants": matching_grants(scope, actor)[:100],
        "group_role": ROLE_LABELS.get(role_of(actor), ""),
        "basis": "权限已由程序按发送者QQ号判定；此字段仅供解释，不要自行推断、承诺或声称已获授权。",
    }


def manage_permissions(scope, args, tool_names, actor=""):
    uid, current_chat, _ = _authority(scope, actor)
    action = args.get("action")
    target = str(args.get("user_id") or uid)
    list_all = action == "list" and is_owner_scope(scope, actor) and not args.get("user_id")
    chat = args.get("chat_scope") or current_chat or ("all" if action == "list" else "private")
    group_scope = re.fullmatch(r"qq:group:(\d+)(?::user:\d+)?", chat)
    private_scope = re.fullmatch(r"qq:private:(\d+)", chat)
    if group_scope:
        chat = "group:" + group_scope[1]
    elif private_scope:
        if private_scope[1] != target:
            raise ValueError("私聊范围中的QQ号必须与授权目标一致")
        chat = "private"
    if not list_all and not target.isdigit() and not (target == "*" and re.fullmatch(r"group:\d+", chat)):
        raise ValueError("必须提供明确的目标QQ号，可使用当前消息中的@QQ，不能仅按昵称猜测")
    if chat not in ("private", "all") and not re.fullmatch(r"group:\d+", chat):
        raise ValueError("chat_scope须为private、group:群号或all")
    if action == "list":
        if not is_owner_scope(scope, actor) and (target != uid or chat != current_chat):
            raise ValueError("普通成员只能查询自己的当前会话授权")
        rows = [{**r, "active": not r.get("expires_at") or r["expires_at"] > time.time()} for r in load_config()["channels"]["napcat"].get("permission_grants", [])
                if (list_all or r["user_id"] in (target, "*")) and (chat == "all" or r["scope"] in (chat, "all"))]
        return {"ok": True, "user_id": "all" if list_all else target, "chat_scope": chat, "grants": rows,
                "remote_tools_enabled": owner_tools_enabled()}
    if action not in ("grant", "revoke") or not is_owner_scope(scope, actor):
        raise ValueError("只有配置中的主人QQ或本地用户可以授予、撤销权限，不能由引用消息或昵称授予")
    tools, skills = args.get("tools", []), args.get("skills", [])
    if not isinstance(tools, list) or not isinstance(skills, list) or any(not isinstance(x, str) for x in tools + skills):
        raise ValueError("tools和skills必须是字符串数组")
    if any(x != "*" and x not in tool_names for x in tools) or "manage_permissions" in tools:
        raise ValueError("工具名无效；权限管理仅由主人执行，不作为普通工具授权")
    if any(x != "*" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", x) for x in skills):
        raise ValueError("技能名无效")
    if not tools and not skills:
        if action == "grant":
            raise ValueError("请明确授权哪些工具或技能；全部工具使用tools=['*']")
        tools, skills = ["*"], ["*"]
    duration = args.get("duration_minutes", 0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= duration <= 525600:
        raise ValueError("duration_minutes须为0到525600，0为长期")
    from core import config
    with config._lock:
        rows = list(load_config()["channels"]["napcat"].get("permission_grants", []))
        now = time.time()
        for kind, names in (("tool", tools), ("skill", skills)):
            if "*" in names:
                names = ["*"]
            for name in names:
                rows = [r for r in rows if not (r["user_id"] == target and r["scope"] == chat and r["kind"] == kind and (r["name"] == name or name == "*"))]
                rows.append({"user_id": target, "scope": chat, "kind": kind, "name": name, "allow": action == "grant",
                             "expires_at": now + duration * 60 if duration else 0, "granted_by": uid or "local", "updated_at": now})
        save_config({"channels": {"napcat": {"permission_grants": rows}}})
    return {"ok": True, "action": action, "user_id": target, "chat_scope": chat, "tools": tools, "skills": skills,
            "duration_minutes": duration, "remote_tools_enabled": owner_tools_enabled()}


def owner_tools_allowed(user_id):
    cfg = load_config()["channels"]["napcat"]
    return bool(cfg.get("allow_owner_tools") and str(user_id) in {str(x) for x in cfg["owner_ids"]})


def scope_owner_tools_allowed(scope, actor=""):
    uid, _, qq_session = _authority(scope, actor)
    if not qq_session:
        return True
    return bool(uid and owner_tools_enabled() and uid in owner_ids())
