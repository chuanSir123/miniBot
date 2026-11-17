"""Owner tool authority comes from the current sender ID, never quoted content.

权限判断的唯一依据是「当前这条消息的发送者 QQ 号」（actor）+ 配置里的授权表。
模型（VLM）和决策模型（Jev）只负责理解意图、挑动作，不参与授权判定：
它们看到的 permissions 只是程序判定结果的转述，即使被引用文本或昵称带偏，
真正执行时仍会在 _execute_tool 里按 actor 重新判定一次。

默认不授权：除 BASE_TOOLS 外，任何能力都要主人显式授予。
主人本身不做逐项判断，只受 owner_ids 约束，没有额外的总开关。

pet / web / 本地工作空间不是 QQ 会话，直接放行，不做权限判断。
"""
import hashlib
import json
import re
import secrets
import threading
import time

from core.config import load_config, save_config

BASE_TOOLS = {"read_history", "schedule_reminder", "list_reminders", "list_skills",
              "list_media", "get_media", "update_reminder", "remember_fact",
              "forget_fact", "notification_feedback", "undo_memory", "manage_goal",
              "read_chat_history"}

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

_PENDING_TTL = 300
_pending = {}
_pending_lock = threading.Lock()


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
            if r["user_id"] in (uid, "*") and r["scope"] in ("all", chat)
            and (not r.get("expires_at") or r["expires_at"] > now)]


def _directive(scope, kind, name, actor=""):
    uid, chat, _ = _authority(scope, actor)
    rows = [r for r in matching_grants(scope, actor)
            if r["kind"] == kind and r["name"] in ("*", name)]
    if not rows:
        return None
    row = max(rows, key=lambda r: (r["scope"] == chat, r["user_id"] == uid,
                                   r["name"] == name, r.get("updated_at", 0)))
    return row["allow"]


def grant_allowed(scope, actor=""):
    """消息准入用：当前身份是否至少有一项有效授权。

    与执行期一致按最具体的指令判定，避免「全员授权 + 单独撤销某人」时
    被其它 allow 行重新放行。
    """
    uid, chat, qq_session = _authority(scope, actor)
    if not qq_session:
        return True
    rows = matching_grants(scope, actor)
    if not rows:
        return False
    row = max(rows, key=lambda r: (r["scope"] == chat, r["user_id"] == uid,
                                   r["name"] != "*", r.get("updated_at", 0)))
    return bool(row["allow"])


def _subject(name, args):
    """工具名折算成授权维度。bash 跑纯技能脚本时按技能名判定，避免一授技能
    就开放任意命令；识别不出来的命令仍按 bash 工具判定。"""
    if name == "bash":
        from core.permission_deps import skill_from_command

        skill = skill_from_command((args or {}).get("command", ""))
        if skill:
            return "skill", skill
    if name == "read":
        from core.permission_deps import is_skill_doc

        if is_skill_doc((args or {}).get("file_path", "")):
            return "tool", ":skill_doc:"
    return "tool", name


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
        # 查询对所有人开放；授予与撤销只认配置里的主人QQ。
        return is_owner_scope(scope, actor) or args.get("action") == "list"
    if is_owner_scope(scope, actor):
        return True
    kind, subject = _subject(name, args)
    directive = _directive(scope, kind, subject, actor)
    if directive is False:
        # 显式撤销（allow=False）对任何身份都生效，也压过全员授权。
        return False
    if kind == "tool" and (subject in BASE_TOOLS or subject == ":skill_doc:"):
        return True
    return directive is True


def permission_summary(scope, actor=""):
    """给模型/Jev 的权限转述；只是解释，不是判定入口。"""
    uid, _, qq_session = _authority(scope, actor)
    owner = is_owner_scope(scope, actor)
    return {
        "qq_session": qq_session,
        "actor_user_id": uid,
        "owner": owner,
        "full_tools": (not qq_session) or owner,
        "default_tools": sorted(BASE_TOOLS),
        "grants": matching_grants(scope, actor)[:100],
        "group_role": ROLE_LABELS.get(role_of(actor), ""),
        "basis": "权限已由程序按发送者QQ号判定；默认不授权，需主人显式授予工具或技能。"
                 "此字段仅供解释，不要自行推断、承诺或声称已获授权。",
    }


def _normalize_subjects(tools, skills, valid_tools):
    """把入参折算成 [(kind, name)]，技能名统一成 frontmatter 的名字。"""
    from core.permission_deps import normalize_skill

    if any(x != "*" and x not in valid_tools for x in tools) or "manage_permissions" in tools:
        raise ValueError("工具名无效；权限管理仅由主人执行，不作为普通工具授权")
    if any(x != "*" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", x) for x in skills):
        raise ValueError("技能名无效")
    normalized = []
    for name in tools:
        normalized.append(("tool", name))
    for name in skills:
        if name == "*":
            normalized.append(("skill", "*"))
            continue
        skill = normalize_skill(name)
        if not skill:
            raise ValueError(f"技能 {name} 不存在，先用 list_skills 确认")
        normalized.append(("skill", skill))
    return list(dict.fromkeys(normalized))


def _dependencies(subjects):
    """实时分析待授权能力的权限依赖，返回去重后的清单。"""
    from core.permission_deps import analyze_dependencies, skill_catalog

    deps, seen = [], set()
    for kind, name in subjects:
        if name == "*":
            targets = ([("skill", n) for n in sorted(skill_catalog())]
                       if kind == "skill" else [])
        else:
            targets = [(kind, name)]
        for dep_kind, dep_name in targets:
            for row in analyze_dependencies(dep_kind, dep_name):
                key = (row["kind"], row["name"])
                if key in seen or key in subjects:
                    continue
                seen.add(key)
                deps.append(row)
    return deps


def _pending_fingerprint(actor, target, chat, subjects, duration):
    raw = json.dumps([actor, target, chat, subjects, duration],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue_pending(actor, target, chat, subjects, duration):
    token = secrets.token_urlsafe(18)
    with _pending_lock:
        now = time.time()
        for key in [k for k, row in _pending.items() if row["expires_at"] <= now]:
            _pending.pop(key, None)
        _pending[token] = {
            "fingerprint": _pending_fingerprint(actor, target, chat, subjects, duration),
            "expires_at": now + _PENDING_TTL,
        }
    return token


def _consume_pending(token, actor, target, chat, subjects, duration):
    if not token:
        return False
    with _pending_lock:
        row = _pending.pop(token, None)
    if not row or row["expires_at"] <= time.time():
        return False
    expected = _pending_fingerprint(actor, target, chat, subjects, duration)
    return secrets.compare_digest(row["fingerprint"], expected)


def manage_permissions(scope, args, valid_tools, actor=""):
    uid, current_chat, _ = _authority(scope, actor)
    action = args.get("action")
    target = str(args.get("user_id") or uid or "")
    is_owner = is_owner_scope(scope, actor)
    list_all = action == "list" and is_owner and not args.get("user_id")
    chat = args.get("chat_scope") or current_chat or ("all" if action == "list" else "private")
    group_scope = re.fullmatch(r"qq:group:(\d+)(?::user:\d+)?", str(chat))
    private_scope = re.fullmatch(r"qq:private:(\d+)", str(chat))
    if group_scope:
        chat = "group:" + group_scope[1]
    elif private_scope:
        if private_scope[1] != target:
            raise ValueError("私聊范围中的QQ号必须与授权目标一致")
        chat = "private"
    if action == "list":
        rows = load_config()["channels"]["napcat"].get("permission_grants", [])
        if not is_owner:
            if target != uid:
                raise ValueError("普通成员只能查询自己或全员授权")
            if chat not in ("all", current_chat):
                raise ValueError("普通成员只能查询当前会话或全员授权")
            rows = [r for r in rows if r["user_id"] in (target, "*")]
        visible = [r for r in rows
                   if (list_all or r["user_id"] in (target, "*"))
                   and (chat == "all" or r["scope"] in (chat, "all"))]
        return {"ok": True, "user_id": "all" if list_all else target, "chat_scope": chat,
                "grants": [{**r, "active": not r.get("expires_at") or r["expires_at"] > time.time()}
                           for r in visible]}
    if action not in ("grant", "revoke") or not is_owner:
        raise ValueError("只有配置中的主人QQ或本地用户可以授予、撤销权限，不能由引用消息或昵称授予")
    if target != "*" and not target.isdigit():
        raise ValueError("必须提供明确的目标QQ号，或使用*开放给所有人，不能仅按昵称猜测")
    if target == "*" and chat == "private":
        raise ValueError("全员授权不能限定到私聊，请用 group:群号 或 all")
    if chat not in ("private", "all") and not re.fullmatch(r"group:\d+", str(chat)):
        raise ValueError("chat_scope须为private、group:群号或all")
    tools, skills = args.get("tools", []), args.get("skills", [])
    if not isinstance(tools, list) or not isinstance(skills, list) or any(
            not isinstance(x, str) for x in tools + skills):
        raise ValueError("tools和skills必须是字符串数组")
    if not tools and not skills:
        if action == "grant":
            raise ValueError("请明确授权哪些工具或技能；全部工具用tools=['*']，全部技能用skills=['*']")
        tools, skills = ["*"], ["*"]
    subjects = _normalize_subjects(tools, skills, valid_tools)
    duration = args.get("duration_minutes", 0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= duration <= 525600:
        raise ValueError("duration_minutes须为0到525600，0为长期")
    if args.get("confirm") and not _consume_pending(
            args.get("confirm_token", ""), actor, target, chat, subjects, duration):
        raise ValueError("确认令牌无效或已过期，请重新发起授权")
    confirmed = bool(args.get("confirm"))
    acknowledged = []
    if action == "grant" and not confirmed:
        dependencies = _dependencies(subjects)
        if dependencies:
            token = _issue_pending(actor, target, chat, subjects, duration)
            return {
                "ok": False,
                "needs_confirmation": True,
                "confirm_token": token,
                "pending": {"user_id": target, "chat_scope": chat, "tools": tools,
                            "skills": skills, "duration_minutes": duration},
                "dependencies": dependencies,
                "message": "该授权存在权限依赖，尚未生效。请把依赖逐条告诉主人，"
                           "等主人明确同意后，带 confirm=true 与 confirm_token 再调用一次；"
                           "不要自行代替主人确认。",
            }
    elif action == "grant" and confirmed:
        acknowledged = _dependencies(subjects)
    # 依赖只用于提醒，绝不自动落库：一次授权不能放大成一组权限。
    rows_to_write = list(subjects)
    from core import config
    with config._lock:
        rows = list(load_config()["channels"]["napcat"].get("permission_grants", []))
        now = time.time()
        for kind, name in rows_to_write:
            rows = [r for r in rows if not (r["user_id"] == target and r["scope"] == chat
                                            and r["kind"] == kind and (r["name"] == name or name == "*"))]
            rows.append({"user_id": target, "scope": chat, "kind": kind, "name": name,
                         "allow": action == "grant",
                         "expires_at": now + duration * 60 if duration else 0,
                         "granted_by": uid or "local", "updated_at": now})
        save_config({"channels": {"napcat": {"permission_grants": rows}}})
    return {"ok": True, "action": action, "user_id": target, "chat_scope": chat,
            "granted": [{"kind": k, "name": n} for k, n in rows_to_write],
            "dependencies_acknowledged": acknowledged,
            "duration_minutes": duration}
