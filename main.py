import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

from core.brain import VLMBrain
from core.config import load_config, subscribe
from core.console import log
from core.harness import AgentHarness
from core.jev import JevClient
from core.memory import MiniMemory
from core.perception import LocalPerception
from core.proactive import ProactiveEngine
from core.runtime import Dispatcher
from core.skill_manager import SkillManager
from webui.server import run_server

# 重启约定：main.py 以这个退出码结束，表示"请把我重新拉起来"。
# launcher.py 看到它就自动重启；没有启动器托管时 main.py 自己拉起新进程。
RESTART_EXIT_CODE = 42
# 启动器打上这个标记，子进程就知道"外面有人看着"，不用自己再拉自己。
SUPERVISED_ENV = "MINIBOT_SUPERVISED"
# 自己拉起时告诉新进程"上一个实例的 PID 是多少"，等它彻底退出再起，避免双开。
PARENT_PID_ENV = "MINIBOT_PARENT_PID"


def _describe_tool(name, args):
    """把一次工具调用压成一行人类可读的描述，文案与 Web 端工具块保持一致。"""
    args = args if isinstance(args, dict) else {}

    def basename(v):
        v = str(v or "").replace("\\", "/").split("/")[-1]
        return v

    def join(title, detail):
        detail = str(detail or "").strip()
        return f"{title} · {detail}" if detail else title

    if name == "execute_command":
        return join("运行了命令", str(args.get("command") or "")[:50])
    if name == "read_file":
        return join("读取了文件", basename(args.get("file_path") or args.get("path")))
    if name == "write_file":
        return join("写入了文件", basename(args.get("file_path") or args.get("path")))
    if name == "edit_file":
        return join("编辑了文件", basename(args.get("file_path") or args.get("path")))
    if name == "capture_screen":
        return join("捕获了屏幕核验效果", str(args.get("reason") or "")[:40])
    if name in ("web_search", "search_web"):
        return join("🌐 已搜索网页", str(args.get("query") or "")[:50])
    if name == "execute_skill":
        skill = str(args.get("skill_name") or "").strip()
        return f"执行了技能 [{skill}]" if skill else "执行了技能"
    return f"调用了工具 [{name}]"


def _describe_tool_delta(data):
    """工具参数的流式增量 → 状态行。只挑有信息量的操作展示，避免每个
    JSON 字符增量都刷一次屏：文件操作显示 +N/-M 行数计数，命令显示内容。"""
    if not isinstance(data, dict):
        return ""
    if data.get("is_file_op"):
        path = str(data.get("file_path") or "").strip()
        action = {"write": "写入", "create": "新建", "overwrite": "覆盖", "edit": "编辑"}.get(
            data.get("action"), "写入"
        )
        added = data.get("lines_added") or 0
        deleted = data.get("lines_deleted") or 0
        counter = f" +{added}/-{deleted}" if (added or deleted) else ""
        return f"✍️ {action} {path}{counter}".strip()
    if data.get("action") == "command":
        cmd = " ".join(str(data.get("snippet") or "").split())
        return f"⌨️ {cmd[:48]}" if cmd else ""
    return ""


class MiniBotApp:
    def __init__(self, mode="pet"):
        self.mode = mode
        self.cfg = load_config()
        self.memory = MiniMemory()
        self.perception = LocalPerception()
        self.jev = JevClient()
        from core.qq_policy import QQPolicy
        self.qq_policy = QQPolicy(self.memory, self.jev)
        self.qq_policy.on_restart = self.request_restart
        self.vlm = VLMBrain()
        self.skill_manager = SkillManager()
        self.harness = AgentHarness(
            self.vlm, self.jev, self.memory, self.perception, self.skill_manager
        )
        runtime = self.cfg["runtime"]
        self.dispatcher = Dispatcher(runtime["max_workers"], runtime["queue_size"])
        from core.chat_compaction import ChatCompactor
        self.qq_policy.chat_compactor = ChatCompactor(self.memory, self.vlm, self.dispatcher)
        self.pet_widget = None
        self.napcat_mgr = None
        self.napcat_channel = None
        self.settings_server = None
        self._stop = threading.Event()
        self._reload_event = threading.Event()
        self._qq_observed = {}
        self._napcat_config = None
        # 重启：收到 restart_bot 工具调用后先记下来，等本轮回复发完再优雅停机。
        self._restart_requested = False
        self._restart_reason = ""
        self._restart_lock = threading.Lock()
        self._qt_app = None
        self.harness.on_restart = self.request_restart
        self.harness.on_completed = lambda goal, trace: self.dispatcher.submit(
            "local:learning",
            self.skill_manager.learn_from_trace,
            goal,
            trace,
            background=True,
        )
        self.proactive = ProactiveEngine(
            self.memory,
            self.perception,
            self.jev,
            self.vlm,
            self.skill_manager,
            self._handle_proactive_notification,
            self.dispatcher,
            self._followup,
            self._verify_proactive,
        )
        self._unsubscribe = subscribe(self._config_changed)

    @property
    def restart_requested(self):
        """是否已经受理过重启请求（决定退出码）。"""
        return self._restart_requested

    @staticmethod
    def identity(event=None):
        if not event:
            # Pet 固定使用 main 空间
            return "main", "main:pet", "pet", "", True
        uid = str(event["user_id"])
        from core.qq_authority import owner_tools_allowed
        tools = owner_tools_allowed(uid)
        cfg_napcat = load_config()["channels"]["napcat"]
        is_owner = uid in {str(x) for x in cfg_napcat.get("owner_ids", [])}
        if event["message_type"] == "group":
            group = str(event["group_id"])
            # 群聊只按群号分组：同一个群的所有人共用一份工作空间、会话历史、
            # 聊天记录与媒体（workspace_for / media_scope 都只取群号这一段）。
            # 发送者 QQ 不写进 scope——它是权限身份（actor），单独传给 harness，
            # 只决定"这个人能执行什么"，不决定"聊天记录落在哪"。
            scope = f"qq_group_{group}"
            session = f"qq:group:{group}"
            return scope, session, "qq_group", group, tools
        # QQ 私聊：如果是主人，直接映射到 main 空间与 main:pet / Web 共享记忆
        if is_owner:
            scope = "main"
            session = f"qq:private:{uid}"
            return scope, session, "qq_private", uid, tools
        # 非主人私聊独立空间
        scope = f"qq_private_{uid}"
        session = f"qq:private:{uid}"
        return scope, session, "qq_private", uid, tools

    def submit_query(
        self,
        text,
        event=None,
        custom_scope=None,
        custom_session=None,
        input_images=None,
        on_text_chunk=None,
        on_status_change=None,
        on_tool_call=None,
        on_tool_result=None,
        on_steering_applied=None,
        on_tool_delta=None,
        on_reasoning_chunk=None,
        on_reasoning_complete=None,
        on_text_reset=None,
        model=None,
        thinking_effort=None,
        user_message_id=None,
        **kwargs,
    ):
        if custom_scope or custom_session:
            scope = custom_scope or "main"
            session = custom_session or f"web:{scope}"
            channel = "web"
            target = ""
            tools = True
        else:
            scope, session, channel, target, tools = self.identity(event)

        # pet 和 qq 强制用主模型；只有 web 渠道支持指定模型与思考强度
        if channel in ("pet", "qq_private", "qq_group") or session.startswith(("pet:", "qq:")):
            selected_model = None
        else:
            te = kwargs.get("thinking_effort", thinking_effort)
            if te is not None:
                selected_model = {
                    "id": model,
                    "enable_thinking": (te != "none"),
                    "reasoning_effort": te,
                }
            else:
                selected_model = model

        norm_text = text.strip()
        cmd = norm_text.lower()
        if cmd == "/cancel":
            self.harness.cancel_session(session)
            count = self.dispatcher.cancel_pending(session)
            future = concurrent.futures.Future()
            reply = f"已取消当前任务及{count}条排队请求。"
            future.set_result(reply)
            if channel == "pet" and self.pet_widget:
                self.pet_widget.finish_say_safe(reply, 3)
            return future

        # 识别是否是主人会话
        from core.qq_authority import is_owner_scope
        owner_ids = {str(x) for x in load_config()["channels"]["napcat"].get("owner_ids", [])}
        is_owner = (
            channel == "pet"
            or (channel == "web" and scope in ("main", "local"))
            or (channel == "qq_private" and (is_owner_scope(target or session or scope) or str(target) in owner_ids))
        )

        # /clear 命令：物理清空历史记录与上下文
        if cmd in ("/clear", "#clear", "clear"):
            self.harness.cancel_session(session)
            self.dispatcher.cancel_pending(session)

            # 需要清理的会话集（主人操作时全渠道共享重置）
            target_sessions = {session}
            if is_owner:
                target_sessions.add("local:pet")
                target_sessions.add("web:main")
                for oid in owner_ids:
                    target_sessions.add(f"qq:private:{oid}")
                try:
                    from webui.server import (
                        ASSISTANT_SESSION_ID,
                        get_active_web_main_sessions,
                    )

                    # 「助理」是各渠道汇聚过来的汇总，清空时要一起清。
                    target_sessions.add(ASSISTANT_SESSION_ID)
                    for s in get_active_web_main_sessions():
                        target_sessions.add(s)
                except Exception:
                    pass

            for s in target_sessions:
                self.memory.clear_session(s)
                self.harness.cancel_session(s)
                try:
                    from core.checkpoint import get_checkpoint_manager
                    get_checkpoint_manager().clear_session(s)
                except Exception:
                    pass
                try:
                    from webui.server import broadcast_sse
                    broadcast_sse(s, "session_cleared", {"session_id": s})
                except Exception:
                    pass

            # 「助理」被清空后要把名字和置顶标记写回去，否则会退化成一条普通会话。
            try:
                from webui.server import ASSISTANT_SESSION_ID, ensure_assistant_session

                if ASSISTANT_SESSION_ID in target_sessions:
                    ensure_assistant_session(self)
            except Exception:
                pass

            future = concurrent.futures.Future()
            reply = "🧹 会话历史与上下文记忆已全部清空，我们可以开始全新话题啦喵！"
            future.set_result(reply)

            if channel == "pet" and self.pet_widget:
                self.pet_widget.finish_say_safe(reply, 4)

            # 跨端同步：如果是主人在 QQ 或 Pet 端清空，同步提示到 Web main
            if channel in ("pet", "qq_private") and is_owner:
                try:
                    from webui.server import broadcast_to_web_main
                    broadcast_to_web_main("complete", {"reply": f"[{'桌宠' if channel=='pet' else 'QQ主人'}] 执行了 /clear：会话记忆已全部清空。"})
                except Exception:
                    pass

            return future

        # /cc 命令：手动压缩当前会话上下文
        if cmd in ("/cc", "#cc", "compact"):
            if hasattr(self.harness, "is_session_active") and self.harness.is_session_active(session):
                future = concurrent.futures.Future()
                reply = "⚠️ 当前任务正在执行中，请在任务完成后再压缩上下文喵。"
                future.set_result(reply)
                if channel == "pet" and self.pet_widget:
                    self.pet_widget.finish_say_safe(reply, 3)
                return future

            from core.compaction import ContextCompactor
            history, _ = self.memory.get_history(session)
            future = concurrent.futures.Future()
            if not history or len(history) < 3:
                reply = "ℹ️ 当前会话历史过短，暂无需压缩喵。"
                future.set_result(reply)
                if channel == "pet" and self.pet_widget:
                    self.pet_widget.finish_say_safe(reply, 3)
                return future

            compactor = ContextCompactor(brain=self.brain, memory=self.memory, session_id=session)
            old_cutoff = compactor.summary["covered_until_id"] if compactor.summary else 0
            ok = compactor._run_compaction_worker(history, keep=6)
            new_cutoff = compactor.summary["covered_until_id"] if compactor.summary else 0
            history_after, _ = self.memory.get_history(session)

            if ok or new_cutoff > old_cutoff:
                reply = f"📦 上下文手动压缩完成！较早的消息已生成结构化摘要归档，当前活跃上下文已精简至最近 {len(history_after)} 条喵。"
            else:
                reply = "ℹ️ 未达到压缩阈值或无多余上下文需要压缩喵。"
            future.set_result(reply)

            if channel == "pet" and self.pet_widget:
                self.pet_widget.finish_say_safe(reply, 4)

            try:
                from webui.server import broadcast_sse
                broadcast_sse(session, "session_compacted", {
                    "session_id": session,
                    "covered_until_id": new_cutoff,
                    "summary": compactor.summary.get("summary", "") if compactor.summary else ""
                })
            except Exception:
                pass

            return future

        # 检查当前空间是否有运行中或上一次任务
        is_running = False
        if hasattr(self.harness, "is_session_active") and callable(self.harness.is_session_active):
            res = self.harness.is_session_active(session)
            is_running = (res is True)

        task_state = self.memory.get_task_state(session) or {}
        task_state["is_running"] = is_running

        # 第一层 Jev 意图判断：通过第一层 Jev 判断是 chat 还是引导等
        # 提示词要说明是否和上次任务相关并且是否要修正或引导上次任务
        jev = getattr(self, "jev", None)
        if jev and hasattr(jev, "classify_steering_intent"):
            intent = jev.classify_steering_intent(text, task_state=task_state)
        else:
            intent = "independent_task"  # No semantic guess or live injection when Jev is unavailable.

        # 1. 引导类型：用于引导上一次任务的就要执行引导
        if intent == "steering":
            if is_running:
                # 任务正在执行中：直接插入引导进行方向修正！
                ok = self.harness.inject_steering(session, text)
                future = concurrent.futures.Future()
                if ok:
                    reply = f"🎯 [实时引导] 已将方向修正指令注入当前执行任务：\n“{text}”"
                else:
                    reply = "引导注入失败，请稍后重试。"
                future.set_result(reply)
                if channel == "pet" and self.pet_widget:
                    self.pet_widget.stream_say_safe(reply)
                return future
            else:
                # 上次任务刚结束或待命：作为对上一次任务的引导和修改继续执行
                dispatch_session = session
        else:
            # 2. 其他类型：应该是异步，支持并发
            if is_running:
                # 若主会话正在忙碌执行长任务，为了不阻塞并支持并发，分配一个独立异步并发会话
                import uuid
                dispatch_session = f"{session}:async:{uuid.uuid4().hex[:6]}"
            else:
                dispatch_session = session

        images = input_images if input_images is not None else (event or {}).get("_parsed", {}).get("images", [])

        return self.dispatcher.submit(
            dispatch_session,
            self._handle_user_query,
            text,
            scope,
            session,  # 真实的存储 session 保持统一
            channel,
            target,
            tools,
            images,
            event,
            extra_on_text=on_text_chunk,
            extra_on_status=on_status_change,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            on_steering_applied=on_steering_applied,
            on_tool_delta=on_tool_delta,
            on_reasoning_chunk=on_reasoning_chunk,
            on_reasoning_complete=on_reasoning_complete,
            on_text_reset=on_text_reset,
            model_override=selected_model,
            user_message_id=user_message_id,
        )

    @staticmethod
    def _mirrors_to_assistant(channel, is_owner):
        """哪些渠道的对话要归档进 Web「助理」：桌宠、QQ 主人私聊。

        QQ 群刻意不进来——群里的对话仍然留在自己的群空间（qq:group:*），
        只有"跟主人本人有关的这几条线"才汇聚到「助理」。
        """
        return channel == "pet" or (channel == "qq_private" and is_owner)

    @staticmethod
    def _assistant_tag(channel, target=""):
        if channel == "pet":
            return "[桌宠]"
        return "[QQ主人]"

    def _handle_user_query(
        self,
        text,
        scope="main",
        session="main:pet",
        channel="pet",
        target="",
        tools=True,
        input_images=None,
        qq_event=None,
        extra_on_text=None,
        extra_on_status=None,
        on_tool_call=None,
        on_tool_result=None,
        on_steering_applied=None,
        on_tool_delta=None,
        on_reasoning_chunk=None,
        on_reasoning_complete=None,
        on_text_reset=None,
        model_override=None,
        user_message_id=None,
    ):
        if self._stop.is_set():
            return "程序正在退出。"
        started = time.monotonic()
        first = [None]
        from core.qq_authority import actor_from_event, is_owner_scope
        # 权限身份只取当前消息的发送者QQ号；pet/web 没有 QQ 事件，actor 为空，
        # 这类本地渠道不做权限判断。工具执行时会按同一个 actor 再判一次。
        actor = actor_from_event(qq_event)
        owner_ids = {str(x) for x in load_config()["channels"]["napcat"].get("owner_ids", [])}
        is_owner = (
            channel == "pet"
            or (channel == "web" and scope in ("main", "local"))
            or (channel == "qq_private" and (is_owner_scope(target or session or scope) or str(target) in owner_ids))
        )

        # QQ、Pet、Web 共享记忆（主空间 facts 与情境事件）
        mem_scope = "main" if is_owner else scope
        _mem_chars = load_config()["memory"]["context_chars"]
        # 记忆召回返回结构化结果（facts / 带 id 的事件 / 相关历史），由流程层决定放哪：
        # 慢变的 facts 和逐条新事件作为**追加消息**进历史（见 core/transcript.py 的
        # memory_entry），此后每轮整段命中前缀缓存，不再每轮重画。
        recall = self.memory.recall_for_turn(
            scope=mem_scope, query=text, max_chars=_mem_chars
        )

        from core.decision_context import build_decision_context, render_qq_turn_context
        decision_context = build_decision_context(self.memory, session, scope, qq_event, actor=actor)
        # QQ 会话身份与近期聊天是**当轮实时状态**，改了就是新内容，放进尾部而不是历史：
        # 它每轮都会变，追加进历史只会让历史无界增长。
        # 逐行渲染（每行自带 昵称(QQ号)），不再 json.dumps 后按字符硬截——那样会
        # 把某条消息从中间切断，模型读到坏 JSON，分不清谁说了什么。
        tail_context = ""
        if qq_event:
            tail_context = render_qq_turn_context(decision_context, budget=_mem_chars)
        self.memory.auto_extract_facts_from_text(text, mem_scope)
        self.memory.add_event(
            "用户：" + text, source=channel, importance=1.5, scope=mem_scope
        )

        # 需求3：QQ 主人 QQ、QQ 群、Pet 端的对话同步到 Web 的 main「助理」会话
        if self._mirrors_to_assistant(channel, is_owner):
            try:
                from webui.server import sync_message_to_web_main
                sync_message_to_web_main(self, "user", f"{self._assistant_tag(channel, target)} {text}", input_images)
            except Exception:
                pass

        is_pet_or_main = (
            channel == "pet"
            or (channel == "web" and scope in ("main", "local"))
            or (channel == "qq_private" and is_owner)
        )

        # 需求3：Web 端 main、Pet、QQ 主人发送消息时，桌宠做思考动作并显示状态
        if is_pet_or_main and self.pet_widget:
            self.pet_widget.start_task_safe(text[:20] if text else "MiniBot", "正在思考…")
            self.pet_widget.set_action(6, duration_ms=4000)

        def on_text(delta, full):
            if first[0] is None:
                first[0] = time.monotonic() - started
            # 需求3：Web 端 main、Pet、QQ 主人回复实时流式同步到桌宠气泡
            if is_pet_or_main and self.pet_widget:
                self.pet_widget.stream_say_safe(full)
            # QQ 主人与 Pet 端回复流式同步到 Web main
            if self._mirrors_to_assistant(channel, is_owner):
                try:
                    from webui.server import broadcast_to_web_main
                    broadcast_to_web_main("text_delta", {"delta": delta, "full": full})
                except Exception:
                    pass
            if extra_on_text:
                try:
                    extra_on_text(delta, full)
                except Exception:
                    pass

        def status(message):
            if is_pet_or_main and self.pet_widget:
                self.pet_widget.set_task_safe(text[:20] if text else "MiniBot", message)
            if self._mirrors_to_assistant(channel, is_owner):
                try:
                    from webui.server import broadcast_to_web_main
                    broadcast_to_web_main("status", {"status": message, "message": message})
                except Exception:
                    pass
            if extra_on_status:
                try:
                    extra_on_status(message)
                except Exception:
                    pass

        def reset_text(reason=""):
            # 草稿被作废（核验重试/连接重试/引导打断）：桌宠同样要丢掉
            # 这一段未定稿的流式文本，避免重生成后新旧内容混在一起。
            if is_pet_or_main and self.pet_widget:
                try:
                    self.pet_widget.reset_stream_safe()
                except Exception:
                    pass
            if self._mirrors_to_assistant(channel, is_owner):
                try:
                    from webui.server import broadcast_to_web_main
                    broadcast_to_web_main("text_reset", {"reason": reason})
                except Exception:
                    pass
            if on_text_reset:
                try:
                    on_text_reset(reason)
                except Exception:
                    pass

        from core.reminders import parse_reminder

        try:
            reminder = parse_reminder(text)
        except ValueError as exc:
            reminder = None
            reply = str(exc)
        else:
            if reminder:
                due, message = reminder
                task_id = self.memory.add_delayed_intent(
                    due,
                    message,
                    scope=scope,
                    session_id=session,
                    channel=channel,
                    target=target,
                    dedupe_key="explicit:"
                    + __import__("hashlib")
                    .sha256(
                        json.dumps(
                            [session, text, int(due) // 60], ensure_ascii=False
                        ).encode()
                    )
                    .hexdigest(),
                )
                import datetime

                reply = f"提醒 #{task_id} 已设置：{datetime.datetime.fromtimestamp(due).strftime('%m-%d %H:%M:%S')}，{message}"
                self.memory.append_message(session, {"role": "user", "content": text})
                self.memory.append_message(
                    session, {"role": "assistant", "content": reply}
                )
            elif text.strip() in ("/mute", "/resume"):
                value = "mute" if text.strip() == "/mute" else "resume"
                self.memory.feedback(scope, "notification", value)
                if scope == "local":
                    from core.config import save_config

                    save_config({"proactive": {"muted": value == "mute"}})
                reply = (
                    "已暂停主动打扰，显式提醒仍会按时投递。"
                    if value == "mute"
                    else "已恢复主动协同。"
                )
            elif text.strip() == "/reminders":
                reply = json.dumps(
                    self.memory.list_intents(scope, session), ensure_ascii=False
                )
            elif text.strip().startswith("/cancel-reminder "):
                try:
                    ok = self.memory.update_intent(
                        int(text.split()[-1]), scope, session, cancel=True
                    )
                except ValueError:
                    ok = False
                reply = "提醒已取消。" if ok else "未找到当前会话中可取消的提醒。"
            else:
                def _wrapped_tool_call(name, args):
                    if isinstance(qq_event, dict):
                        qq_event["_used_tools"] = True
                    # 桌宠与 Web 一样在正文里回显"执行了什么工具/技能"，
                    # 不再只走状态行——文本流和活动行分段共存，互不覆盖。
                    if is_pet_or_main and self.pet_widget:
                        desc = _describe_tool(name, args)
                        try:
                            self.pet_widget.append_activity_safe(desc)
                        except Exception:
                            pass
                        try:
                            self.pet_widget.set_task_safe(
                                text[:20] if text else "MiniBot", desc
                            )
                        except Exception:
                            pass
                    if self._mirrors_to_assistant(channel, is_owner):
                        try:
                            from webui.server import broadcast_to_web_main
                            broadcast_to_web_main("tool_call", {"tool": name, "args": args})
                        except Exception:
                            pass
                    if on_tool_call:
                        try:
                            on_tool_call(name, args)
                        except Exception:
                            pass

                def _wrapped_tool_delta(data):
                    if isinstance(qq_event, dict):
                        qq_event["_used_tools"] = True
                    # 文件写入/命令执行等参数流式增量：桌宠状态行实时显示
                    # "✍️ 编辑 xx.py +12/-3" 这类进度（非文件类增量不刷屏）。
                    if is_pet_or_main and self.pet_widget:
                        activity = _describe_tool_delta(data)
                        if activity:
                            try:
                                self.pet_widget.set_task_safe(
                                    text[:20] if text else "MiniBot", activity
                                )
                            except Exception:
                                pass
                    if self._mirrors_to_assistant(channel, is_owner):
                        try:
                            from webui.server import broadcast_to_web_main
                            broadcast_to_web_main("tool_delta", data)
                        except Exception:
                            pass
                    if on_tool_delta:
                        try:
                            on_tool_delta(data)
                        except Exception:
                            pass

                def _wrapped_tool_result(name, result):
                    # 桌宠收尾：给刚才那条工具活动行打 ✅/⚠️，状态行同步结果。
                    if is_pet_or_main and self.pet_widget:
                        ok = result.get("ok", True) if isinstance(result, dict) else True
                        try:
                            self.pet_widget.complete_activity_safe(ok)
                        except Exception:
                            pass
                        try:
                            self.pet_widget.set_task_safe(
                                text[:20] if text else "MiniBot",
                                ("✅ 完成：" + name) if ok else ("⚠️ 出错：" + name),
                            )
                        except Exception:
                            pass
                    if self._mirrors_to_assistant(channel, is_owner):
                        try:
                            from webui.server import broadcast_to_web_main
                            broadcast_to_web_main("tool_result", {"tool": name, "result": result})
                        except Exception:
                            pass
                    if on_tool_result:
                        try:
                            on_tool_result(name, result)
                        except Exception:
                            pass

                def _wrapped_reasoning_chunk(delta, full):
                    if self._mirrors_to_assistant(channel, is_owner):
                        try:
                            from webui.server import broadcast_to_web_main
                            broadcast_to_web_main("reasoning_delta", {"delta": delta, "full": full})
                        except Exception:
                            pass
                    if on_reasoning_chunk:
                        try:
                            on_reasoning_chunk(delta, full)
                        except Exception:
                            pass

                def _wrapped_reasoning_complete(full):
                    if self._mirrors_to_assistant(channel, is_owner):
                        try:
                            from webui.server import broadcast_to_web_main
                            broadcast_to_web_main("reasoning_complete", {"full": full})
                        except Exception:
                            pass
                    if on_reasoning_complete:
                        try:
                            on_reasoning_complete(full)
                        except Exception:
                            pass

                def _wrapped_steering_applied(steer_text):
                    if self._mirrors_to_assistant(channel, is_owner):
                        try:
                            from webui.server import broadcast_to_web_main
                            broadcast_to_web_main("steering_applied", {"text": steer_text})
                        except Exception:
                            pass
                    if on_steering_applied:
                        try:
                            on_steering_applied(steer_text)
                        except Exception:
                            pass

                reply = self.harness.run(
                    text,
                    on_text_chunk=on_text,
                    on_status_change=status,
                    session_id=session,
                    scope=scope,
                    channel=channel,
                    target=target,
                    allow_tools=tools,
                    actor=actor,
                    memory_recall=recall,
                    tail_context=tail_context,
                    input_images=input_images,
                    decision_context=decision_context,
                    on_intermediate=(qq_event or {}).get("_on_intermediate") if channel in ("qq_private", "qq_group") else None,
                    on_tool_call=_wrapped_tool_call,
                    on_tool_result=_wrapped_tool_result,
                    on_steering_applied=_wrapped_steering_applied,
                    on_tool_delta=_wrapped_tool_delta,
                    on_reasoning_chunk=_wrapped_reasoning_chunk,
                    on_reasoning_complete=_wrapped_reasoning_complete,
                    on_text_reset=reset_text,
                    model_override=model_override,
                    user_message_id=user_message_id,
                )
        self.memory.add_event("助手：" + reply, source="bot", scope=mem_scope)
        # 需求3：Web 端 main、Pet、QQ 主人的回复同步到 Pet 端发送（桌宠气泡播报）
        if is_pet_or_main and self.pet_widget:
            self.pet_widget.finish_say_safe(reply, 3)
        # 需求3：Web 端 main、Pet、QQ 主人回复同步记录到 Web main「助理」会话
        if self._mirrors_to_assistant(channel, is_owner):
            try:
                from webui.server import sync_message_to_web_main
                sync_message_to_web_main(self, "assistant", reply)
            except Exception:
                pass
        print(
            json.dumps(
                {
                    "metric": "query",
                    "channel": channel,
                    "duration_sec": round(time.monotonic() - started, 3),
                    "first_text_sec": round(first[0], 3)
                    if first[0] is not None
                    else None,
                },
                ensure_ascii=False,
            )
        )
        return reply

    def _notify_qq(self, channel, target, message):
        """往 QQ 发一条通知；目标不在白名单里或 NapCat 未连接时静默跳过。"""
        if not self.napcat_channel:
            return False
        cfg = load_config()["channels"]["napcat"]
        target = str(target or "")
        if not target:
            return False
        if channel == "qq_group":
            # 群的可发范围与 NapCatChannel.eligible 保持一致：开放群 or 明确授权的群
            allowed = {str(x) for x in cfg.get("allowed_group_ids", [])}
            permitted = cfg.get("group_allow_all", False) or target in allowed
        else:
            allowed = {
                str(x)
                for x in (cfg.get("owner_ids") or []) + (cfg.get("allowed_user_ids") or [])
            }
            permitted = target in allowed
        if not permitted:
            return False
        try:
            return bool(self.napcat_channel.send_notification(channel, target, message))
        except Exception as exc:
            print("[Notification]", exc)
            return False

    def _record_in_assistant(self, role, text, source=""):
        """把一条渠道消息归档进 Web main 空间置顶的「助理」会话。"""
        try:
            from webui.server import record_assistant_message

            return record_assistant_message(self, role, text, source=source) is not None
        except Exception as exc:
            print("[助理]", exc)
            return False

    def _handle_proactive_notification(
        self, message, posture="general", anim_action=3, task=None
    ):
        """主动发言的统一出口：桌宠气泡 + QQ 主人 + Web「助理」三处同步。

        task["scope"] == "local" 表示这是本地桌面的主动提醒（久坐/报错/饭点…），
        这种提醒要同时推给 QQ 主人，并在「助理」里留一条记录；
        scope 是 QQ 群时只回原渠道，不进「助理」——群里的对话留在自己的群空间。
        """
        task = task or {"channel": "pet", "target": ""}
        pcfg = load_config()["proactive"]
        local_proactive = task.get("scope", "local") == "local"
        delivered = False

        if task["channel"] == "pet":
            if self.pet_widget:
                self.pet_widget.set_task_safe("主动提醒", "有新消息")
                self.pet_widget.finish_say_safe(message, anim_action)
                delivered = True
            elif self.mode == "local":
                print("[主动提醒] " + message)
                delivered = True
        elif task["channel"] in ("qq_private", "qq_group"):
            delivered = self._notify_qq(task["channel"], task["target"], message)

        # 本地主动提醒再私聊主人一份：人不在电脑前时，桌宠气泡是看不见的。
        if local_proactive and pcfg.get("notify_qq_owner", True):
            owners = load_config()["channels"]["napcat"].get("owner_ids", [])
            for owner in owners:
                delivered = self._notify_qq("qq_private", owner, message) or delivered

        # 所有"跟主人本人有关"的主动发言都进「助理」，Web 端能看到它什么时候主动说了什么。
        # QQ 群的发言不进——群里的对话仍然留在自己的群空间。
        owners = {str(x) for x in load_config()["channels"]["napcat"].get("owner_ids", [])}
        owner_private = task["channel"] == "qq_private" and str(task.get("target")) in owners
        if pcfg.get("notify_web_assistant", True) and (
            local_proactive or task["channel"] == "pet" or owner_private
        ):
            if local_proactive or task["channel"] == "pet":
                text = message
            else:
                text = f"[QQ主人] {message}"
            delivered = self._record_in_assistant(
                "assistant", text, source="主动提醒" if not owner_private else "QQ主人"
            ) or delivered

        return delivered

    # 主动提醒的核实提示词。核心是"先看再说"：工具全部放开（截图、读文件、读历史、
    # 跑只读命令都行），但只用提示词约束"这一轮别动手"——核实是为了给建议，
    # 不是为了替主人改东西。
    PROACTIVE_VERIFY_PROMPT = (
        "【主动提醒核实】你准备主动找主人说话。先核实下面这个判断是否成立，再给一句话建议。\n"
        "准备开口的判断：{claim}\n"
        "触发原因：{trigger}\n"
        "桌面状态：{state}\n\n"
        "要求：\n"
        "1. 先核实再看结论。可以截图看屏幕、读文件、读历史，也可以跑只读命令"
        "（git status、dir、type 这类）确认现状；看到什么就以什么为准。\n"
        "2. 结论必须落在你实际看到的内容上，并点出具体依据（窗口标题、界面上的文字、"
        "报错原文、文件里的片段）。\n"
        "3. 看不到依据时，只说你看到了什么、还需要确认什么，不要下结论。"
        "禁止使用「已解决」「已修复」「已经好了」「仍然异常」「仍检测到异常」这类判断词。\n"
        "4. 这一轮只核实，不要主动修改文件、不要执行会改变系统状态的命令、"
        "不要声称已经替主人做了什么。\n"
        "5. 用你平时的口吻说话（像跟主人聊天），别写成核查报告；一到两句话，50 字以内。\n"
        "6. 核实后如果根本不值得打扰（报错早已过去、主人正在正常输入），只回复两个字：静默。"
    )

    def _verify_proactive(self, state, posture, claim):
        """断言型主动消息的核实入口。返回空字符串表示"没有证据，别说话"。

        模型觉得该提醒，必须先用工具看一眼真实屏幕/文件/历史，能指出依据才允许出门。
        工具是全部放开的，靠提示词约束"当轮不改东西"——所以这里把用过的工具记到日志，
        真被改了东西能回溯到是哪一次核实。
        """
        from core.decision_context import build_decision_context

        context = build_decision_context(self.memory, "main:pet", "main")
        state_brief = {
            key: state.get(key)
            for key in (
                "process",
                "title",
                "idle_sec",
                "active_minutes",
                "local_time",
                "trigger",
                "meal_period",
                "late_night",
                "error_signal_recent",
                "environment_events",
                "returned",
            )
        }
        prompt = self.PROACTIVE_VERIFY_PROMPT.format(
            claim=claim,
            trigger=state.get("trigger", ""),
            state=json.dumps(state_brief, ensure_ascii=False),
        )
        used_tools = []

        def note_tool(name, args):
            used_tools.append(name)

        print("[主动核实] 开始 session=local:proactive:verify claim=" + str(claim)[:40])
        try:
            # 独立会话：核实过程不写进桌宠的对话历史，也不会抢占 main:pet 的活动锁。
            # 上下文靠 decision_context 带进去，所以它仍然知道刚聊过什么。
            reply = self.harness.run(
                prompt,
                "",
                session_id="local:proactive:verify",
                scope="local",
                channel="pet",
                target="",
                allow_tools=True,
                readonly=False,
                decision_context=context,
                on_tool_call=note_tool,
            )
        except Exception as exc:
            print("[主动核实]", exc)
            return ""
        if used_tools:
            print(
                "[主动核实] 用过工具："
                + "、".join(dict.fromkeys(used_tools))
                + "（本轮要求只核实不改动，如有变更需回查）"
            )
        reply = (reply or "").strip()
        if not reply or reply.startswith(("【VLM", "【提示")):
            return ""
        if reply.strip("。.！! ") in ("静默", "保持静默"):
            return ""
        return reply

    def _followup(self, task, state):
        """跟进型主动消息：和普通主动提醒走同一套只读核实。"""
        if task["scope"] != "local":
            return task["action_prompt"]
        return self._verify_proactive(state, "debug_assist", task["action_prompt"])

    def _observe_qq(self, text, event):
        # Only allowlisted group members reach this function. No observed text gains tools.
        scope, session, channel, target, _ = self.identity(event)
        self.memory.add_event("群聊旁听：" + text, source="qq_observation", scope=scope)
        now = time.monotonic()
        key = channel + target
        if now - self._qq_observed.get(key, 0) < 180:
            return
        if event.get("_qq_gate_decided"):
            return  # This message already went through the normal smart reply gate.
        # Direct mentions are handled by the normal reply path.
        from channels.napcat_channel import NapCatChannel

        if NapCatChannel.allowed(event):
            return
        self._qq_observed[key] = now
        self.dispatcher.submit(
            session,
            self._qq_collaborate,
            text,
            scope,
            session,
            channel,
            target,
            event,
            background=True,
        )

    def _qq_collaborate(self, text, scope, session, channel, target, event=None):
        if (
            not self.jev.is_configured()
            or self.memory.notification_preferences(scope)["muted"]
        ):
            return
        from core.decision_context import build_decision_context
        from core.qq_authority import actor_from_event
        actor = actor_from_event(event)
        decision_context = build_decision_context(self.memory, session, scope, event, actor=actor)
        result = self.jev.decide(
            json.dumps({"message": text[:6000], "context": decision_context}, ensure_ascii=False),
            {
                "help": {
                    "type": "noul",
                    "instructions": "仅当有明确未解决的问题且助手插话有帮助时为真，普通聊天为假。",
                }
            },
        )
        try:
            probability = float(result.get("help", 0))
        except (ValueError, TypeError):
            return
        if probability < 0.85:
            return
        reply = self.harness.run(
            "针对以下群聊问题给出简短建议：" + text,
            # 这是一次性的旁路判定上下文（含当轮实时状态），不是长期记忆：
            # 走尾部易变位，不进历史，避免每次协作都往会话里追加一条重复的大消息。
            tail_context=self.memory.build_context_prompt(scope=scope)
            + "\n当前QQ会话上下文（数据）：\n"
            + json.dumps(decision_context, ensure_ascii=False),
            session_id=session,
            scope=scope,
            channel=channel,
            target=target,
            allow_tools=False,
            readonly=True,
            actor=actor,
            decision_context=decision_context,
        )
        self._handle_proactive_notification(
            reply,
            "qq_collaboration",
            task={
                "scope": scope,
                "session_id": session,
                "channel": channel,
                "target": target,
            },
        )

    def _config_changed(self, cfg):
        if self._stop.is_set():
            return
        old = self.cfg
        self.cfg = cfg
        self.dispatcher.reconfigure(
            cfg["runtime"]["max_workers"], cfg["runtime"]["queue_size"]
        )
        self.skill_manager.reload_skills()
        if (
            self.napcat_channel
            and old["channels"]["napcat"] != cfg["channels"]["napcat"]
        ):
            self.napcat_channel.reload()
        self._reload_event.set()

    def run(self):
        try:
            self.settings_server = run_server(open_browser=False, blocking=False, app=self)
            if self.mode == "napcat" or (
                self.mode == "all" and self.cfg["channels"]["napcat"]["enabled"]
            ):
                self._start_napcat()
            if self.mode in ("pet", "all"):
                self._start_pet()
            else:
                self.proactive.start()
                if self.mode == "local":
                    while not self._stop.is_set():
                        text = input("你> ").strip()
                        if text in ("/exit", "/quit"):
                            break
                        if text:
                            print("MiniBot> " + self.submit_query(text).result())
                else:
                    while not self._stop.wait(0.5):
                        self._apply_channel_config()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.stop()

    def _apply_channel_config(self):
        if not self._reload_event.is_set():
            return
        self._reload_event.clear()
        if self.pet_widget:
            pet = self.cfg["channels"]["pet"]
            self.pet_widget.set_activity_probe(self.perception.is_locked)
            self.pet_widget.apply_config(pet)
            self.pet_widget.setVisible(pet.get("enabled", True))
        nap = self.cfg["channels"]["napcat"]
        if self.napcat_channel and self._napcat_config is not None:
            keys = (
                "ws_url",
                "access_token",
                "auto_launch",
                "qq_number",
                "qq_path",
                "use_proxy",
            )
            if any(nap.get(k) != self._napcat_config.get(k) for k in keys):
                self.napcat_channel.stop()
                self.napcat_channel = None
                if self.napcat_mgr:
                    self.napcat_mgr.stop()
                    self.napcat_mgr = None
                self._start_napcat()
        if self.mode == "all":
            enabled = self.cfg["channels"]["napcat"].get("enabled", False)
            if enabled and not self.napcat_channel:
                self._start_napcat()
            elif not enabled and self.napcat_channel:
                self.napcat_channel.stop()
                self.napcat_channel = None
                if self.napcat_mgr:
                    self.napcat_mgr.stop()
                    self.napcat_mgr = None

    def _start_pet(self):
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QTimer
        from channels.pet_channel import DesktopPetWidget

        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.aboutToQuit.connect(self.stop)
        self._qt_app = app
        pet = self.cfg["channels"]["pet"]
        path = Path(pet["pet_dir"])
        if not path.is_absolute():
            path = Path(__file__).parent / path

        def on_input(text, input_images=None):
            future = self.submit_query(text, input_images=input_images)

            def finished(done):
                if done.cancelled():
                    return
                if done.exception():
                    self.pet_widget.finish_say_safe(str(done.exception()), 3)

            future.add_done_callback(finished)

        self.pet_widget = DesktopPetWidget(
            str(path), on_input, lambda: run_server(open_browser=True, blocking=False)
        )
        self.pet_widget.set_activity_probe(self.perception.is_locked)
        self.pet_widget.apply_config(pet)
        self.pet_widget.setVisible(pet.get("enabled", True))
        self.proactive.start()
        timer = QTimer()
        timer.timeout.connect(self._apply_channel_config)
        timer.start(500)
        app.exec_()

    def _start_napcat(self):
        from channels.napcat_channel import NapCatChannel
        from channels.napcat_manager import NapCatManager

        cfg = self.cfg["channels"]["napcat"]
        if cfg.get("auto_launch", True):
            self.napcat_mgr = NapCatManager()
            if not self.napcat_mgr.start(cfg.get("qq_number", "")):
                raise RuntimeError("NapCat启动失败，请检查安装或使用外部NapCat")
        self.napcat_channel = NapCatChannel(
            cfg["ws_url"], cfg["access_token"], self.submit_query, self._observe_qq, self.qq_policy
        )
        self.napcat_channel.start()
        self._napcat_config = load_config()["channels"]["napcat"]

    # ------------------------------------------------------------------
    # 重启
    # ------------------------------------------------------------------
    def request_restart(self, reason="", session_id=""):
        """受理重启请求。

        这里**不**立刻杀进程：当前这轮回复还没发出去，立刻停机等于把话咽回去。
        做法是起一个后台线程，等这一轮会话结束（回复已经交给通道去发送），
        再走优雅停机；退出码交给启动器（或自己拉起的新进程）负责重启。
        """
        with self._restart_lock:
            if self._restart_requested:
                return False
            self._restart_requested = True
            self._restart_reason = str(reason or "")[:200]
        log("App", "restart.requested", reason=self._restart_reason, session=session_id)
        threading.Thread(
            target=self._wait_reply_then_stop, args=(session_id,), daemon=True
        ).start()
        return True

    def _wait_reply_then_stop(self, session_id=""):
        # 等当前会话跑完：harness 会话结束 = 文本已经交给通道去发送。
        deadline = time.time() + 60
        while time.time() < deadline:
            if not session_id or not self.harness.is_session_active(session_id):
                break
            time.sleep(0.3)
        # 通道可能还在发消息（文字/图片/表情），等 OneBot 请求排空再退，
        # 否则这条"马上重启"的回复会卡在半路发不出去。
        idle_deadline = time.time() + 20
        while time.time() < idle_deadline:
            if self._qq_sending():
                time.sleep(0.3)
                continue
            time.sleep(0.4)
            if not self._qq_sending():
                break
        time.sleep(1.2)
        self._shutdown_for_restart()

    def _qq_sending(self):
        """QQ 通道是否还有在途的 OneBot 请求。"""
        channel = self.napcat_channel
        if channel is None:
            return False
        try:
            return bool(getattr(channel, "_pending", None))
        except Exception:
            return False

    def _shutdown_for_restart(self):
        log("App", "restart.shutdown", reason=self._restart_reason, mode=self.mode)
        self._stop.set()
        if self._qt_app is not None:
            try:
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(0, self._qt_app.quit)
            except Exception as exc:
                log("App", "restart.quit_error", error=str(exc)[:200])

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._unsubscribe()
        self.harness.stop()
        self.proactive.stop()
        if self.napcat_channel:
            self.napcat_channel.stop()
        if self.napcat_mgr:
            self.napcat_mgr.stop()
        self.dispatcher.shutdown()
        if self.settings_server:
            self.settings_server.shutdown()
            self.settings_server.server_close()


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着（Windows 与 POSIX 各自处理）。"""
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_for_parent_exit(timeout=30.0):
    """自己拉起时，等上一个实例彻底退出，避免两个 bot 同时连 NapCat、抢数据库。"""
    raw = os.environ.get(PARENT_PID_ENV, "")
    if not raw.isdigit():
        return
    pid = int(raw)
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.3)


def _spawn_self_restart(mode: str) -> bool:
    """没有启动器托管时，自己把新进程拉起来（沿用同一个控制台，日志不丢）。"""
    try:
        import subprocess

        env = dict(os.environ)
        env.pop(SUPERVISED_ENV, None)
        env[PARENT_PID_ENV] = str(os.getpid())
        root = Path(__file__).resolve().parent
        cmd = [sys.executable, str(root / "main.py"), "--mode", mode]
        kwargs = {}
        if os.name == "nt":
            # 独立进程组：父进程退出后子进程照旧活着，Ctrl+C 也不会误伤它。
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, cwd=str(root), env=env, **kwargs)
        return True
    except Exception as exc:
        log("App", "restart.spawn_error", error=str(exc)[:200])
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniBot")
    parser.add_argument(
        "--mode", choices=["pet", "napcat", "all", "local"], default="pet"
    )
    mode = parser.parse_args().mode
    _wait_for_parent_exit()
    app = MiniBotApp(mode)
    app.run()
    if app.restart_requested:
        if os.environ.get(SUPERVISED_ENV) == "1":
            # 外面有启动器看着：交出退出码，让它来拉起新实例。
            sys.exit(RESTART_EXIT_CODE)
        if _spawn_self_restart(mode):
            sys.exit(0)
        sys.exit(RESTART_EXIT_CODE)
    sys.exit(0)
