"""
MiniBot WebUI & Harness Server.
Provides:
1. Agent Harness Web Interface (task execution, code workspace exploration, steering guidance).
2. Configuration Center (Jev, VLM, Channels, Proactive settings).
3. Workspace and Session REST APIs + Server-Sent Events (SSE) streaming for real-time tool/text trace.
"""
import base64
import concurrent.futures
import json
import os
import subprocess
import queue
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from core.config import load_config, save_config
from core.console import log
from core.network import urlopen
from core.workspace import (
    create_workspace,
    list_workspace_files,
    list_workspaces,
    read_workspace_file,
    workspace_for,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

WEBUI_DIR = Path(__file__).resolve().parent
HTML_FILE = WEBUI_DIR / "index.html"

# Global SSE broadcast registry: session_id -> list of queue.Queue
_sse_subscribers: Dict[str, List[queue.Queue]] = {}
_sse_lock = threading.Lock()
_active_session_turns: Dict[str, dict] = {}
_active_turns_lock = threading.Lock()
# 每个进行中回合里"当前正在追加到哪个块"，用来保证思考/正文/工具按时间顺序排列。
_active_turn_cursors: Dict[str, dict] = {}
# 用户点过停止的会话；await_reply 据此不再把收尾结果当成正常完成广播出去。
_cancel_requests: Dict[str, float] = {}

# main 空间里置顶的"助理"会话：桌宠、QQ 主人、主动提醒的对话全部汇聚到这一条线上。
# 它不是普通 web 会话——永远存在、置顶、不可删除，是本地渠道与 Web 之间的统一入口。
ASSISTANT_SESSION_ID = "web:main:assistant"
ASSISTANT_TITLE = "助理"


def assistant_session_id(scope="main"):
    if scope in ("main", "local", "", None):
        return ASSISTANT_SESSION_ID
    return f"web:{scope}:assistant"


def ensure_assistant_session(app, scope="main"):
    """保证"助理"会话存在并打上置顶标记，返回会话 id。"""
    sid = assistant_session_id(scope)
    if not app:
        return sid
    try:
        state = app.memory.get_task_state(sid) or {}
        if state.get("original_goal") != ASSISTANT_TITLE or not state.get("pinned"):
            state["original_goal"] = ASSISTANT_TITLE
            state["pinned"] = True
            app.memory.save_task_state(sid, state)
        with app.memory._get_conn() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions_v2 VALUES (?,?)", (sid, time.time())
            )
    except Exception as exc:
        print("Failed to ensure assistant session:", exc)
    return sid


def record_assistant_message(app, role, content, images=None, source=""):
    """把外部渠道（桌宠 / QQ / 主动提醒）的一条消息写进"助理"会话并实时推给前端。

    role: "user"（渠道那边进来的话）或 "assistant"（助手的发言 / 主动提醒）。
    返回落库后的消息；内容为空或写入失败时返回 None。
    """
    if not app or not isinstance(content, str) or not content.strip():
        return None
    sid = ensure_assistant_session(app)
    images = list(images or [])
    try:
        saved = app.memory.append_message(
            sid, {"role": role, "content": content, "images": images}
        )
    except Exception as exc:
        print("Failed to append assistant message:", exc)
        return None
    try:
        if role == "user":
            with _active_turns_lock:
                _active_session_turns[sid] = {
                    "session_id": sid,
                    "scope": "main",
                    "status": "正在处理任务...",
                    "reasoning": "",
                    "reasoning_done": False,
                    "text": "",
                    "tools": [],
                    "blocks": [],
                    "started_at": time.time(),
                    "is_active": True,
                }
                _active_turn_cursors[sid] = {}
            broadcast_sse(
                sid,
                "user_message",
                {
                    "id": (saved or {}).get("_id"),
                    "content": content,
                    "session_id": sid,
                    "images": images,
                },
            )
        else:
            _turn_reset(sid)
            broadcast_sse(
                sid,
                "complete",
                {
                    "reply": content,
                    "artifacts": [],
                    "file_changes": [],
                    "source": source,
                },
            )
    except Exception:
        pass
    return saved


def _turn_start_block(session_id: str, kind: str) -> dict:
    """取（或新建）当前块的引用；同一时刻只允许一个块处于 open 状态。

    调用方必须已持有 _active_turns_lock。
    """
    turn = _active_session_turns.get(session_id)
    if turn is None:
        return {}
    cursor = _active_turn_cursors.setdefault(session_id, {})
    current = cursor.get(kind)
    if current and current.get("open"):
        return current
    for other, block in list(cursor.items()):
        if block is not None:
            block["open"] = False
            cursor[other] = None
    block = {"type": kind, "open": True}
    if kind == "tools":
        block["items"] = []
    else:
        block["text"] = ""
    turn.setdefault("blocks", []).append(block)
    cursor[kind] = block
    return block


def _turn_end_block(session_id: str, kind: str) -> None:
    """关闭当前块。调用方必须已持有 _active_turns_lock（该锁不可重入）。"""
    cursor = _active_turn_cursors.get(session_id) or {}
    block = cursor.get(kind)
    if block is not None:
        block["open"] = False
        cursor[kind] = None


def _turn_reset(session_id: str) -> None:
    with _active_turns_lock:
        _active_session_turns.pop(session_id, None)
        _active_turn_cursors.pop(session_id, None)



# 只给模型看的内部条目类型：记忆召回、Harness 执行期提示。它们以 user 身份落库，
# 但绝不能出现在聊天流里（否则界面上会冒出一大段"用户明确记忆/近期事件"）。
_INTERNAL_MESSAGE_KINDS = ("memory", "notice")


def _is_internal_message(msg) -> bool:
    meta = msg.get("meta") or {}
    if str(meta.get("kind") or "") in _INTERNAL_MESSAGE_KINDS:
        return True
    return str(msg.get("role") or "") in ("system",)


def _filter_display_messages(messages):
    """过滤掉不适合展示的条目（记忆 / Harness 提示 / system），折叠历史里的重复命令。

    模型投影仍然需要这些条目，所以只在这里对"回显给前端"的列表做过滤。
    """
    visible = [m for m in (messages or []) if not _is_internal_message(m)]
    return _collapse_adjacent_duplicate_prompts(visible)


def _collapse_adjacent_duplicate_prompts(messages):
    """折叠"同一句用户输入连续出现两次"的旧数据。

    记忆条目的存在曾让去重失效，同一句话被落库两次（中间只夹着一条记忆条目）。
    那批历史已经写进库里，不再改数据，只在回显时折叠掉后一条；
    真正的重复发言一定隔着助手回复，不会被误折叠。
    """
    collapsed = []
    last_text = None
    nothing_since_last_user = False
    for msg in messages:
        if msg.get("role") == "user":
            text = msg.get("content")
            key = text.strip() if isinstance(text, str) else None
            if nothing_since_last_user and key and key == last_text:
                continue
            last_text = key
            nothing_since_last_user = True
            collapsed.append(msg)
            continue
        nothing_since_last_user = False
        collapsed.append(msg)
    return collapsed


def _drop_duplicated_steerings(messages, active_turn):
    """去掉与进行中轮次重复的引导消息。

    进行中的这轮已经由 active_turn.blocks 精确重建（引导插在生效位置），
    历史里同一条消息既会重复出现，又会被排到整轮之前。
    """
    if not active_turn:
        return messages
    texts = [
        (b.get("text") or "").strip()
        for b in (active_turn.get("blocks") or [])
        if b.get("type") == "steering" and (b.get("text") or "").strip()
    ]
    if not texts:
        return messages
    return [
        m for m in messages
        if not (m.get("is_steering") and any(t in str(m.get("content") or "") for t in texts))
    ]


def _sanitize_for_json(obj):
    """Recursively clean dicts/lists to ensure JSON serializability (omit raw bytes and private _ fields)."""
    if isinstance(obj, bytes):
        return f"<bytes {len(obj)}>"
    if isinstance(obj, dict):
        return {
            k: _sanitize_for_json(v)
            for k, v in obj.items()
            if not (k.startswith("_") and k not in ("_id", "_created_at", "_status"))
        }
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_for_json(item) for item in obj]
    return obj


def safe_json_dumps(obj, **kwargs):
    """Serialize obj to JSON without failing on bytes or non-serializable objects."""
    kwargs.setdefault("ensure_ascii", False)
    try:
        return json.dumps(obj, default=lambda o: f"<bytes {len(o)}>" if isinstance(o, bytes) else str(o), **kwargs)
    except Exception:
        sanitized = _sanitize_for_json(obj)
        return json.dumps(sanitized, default=str, **kwargs)


def register_sse_subscriber(session_id: str) -> queue.Queue:
    # 512 个事件约等于十几秒的文本增量；再挤就说明前端没在消费，丢弃也不会丢字
    # （text_delta 自带 full，下一帧即可自愈）。
    # 2048 个事件足够容纳高频工具调用与代码生成流式增量
    q = queue.Queue(maxsize=2048)
    with _sse_lock:
        _sse_subscribers.setdefault(session_id, []).append(q)
    return q


def unregister_sse_subscriber(session_id: str, q: queue.Queue):
    with _sse_lock:
        if session_id in _sse_subscribers:
            try:
                _sse_subscribers[session_id].remove(q)
            except ValueError:
                pass
            if not _sse_subscribers[session_id]:
                _sse_subscribers.pop(session_id, None)


def broadcast_sse(session_id: str, event_type: str, data: dict):
    clean_data = _sanitize_for_json(data)
    with _sse_lock:
        targets = list(_sse_subscribers.get(session_id, []))
    for q in targets:
        try:
            q.put_nowait({"event": event_type, "data": clean_data})
        except queue.Full:
            pass


def get_active_web_main_sessions():
    """获取所有当前有活跃 SSE 连接的 web:main: 会话列表；若无则返回默认会话。"""
    with _sse_lock:
        sids = [sid for sid in _sse_subscribers.keys() if sid.startswith("web:main:")]
        if not sids:
            return ["web:main:default"]
        return list(set(sids + ["web:main:default"]))


def sync_message_to_web_main(app, role: str, content: str, images: list = None):
    """将 QQ 主人或 Pet 端的发言与回复同步到 Web 端 main 空间的"助理"会话。

    以前是往"当前所有打开的 web:main:* 会话"里各写一份，导致历史被复制进多条
    互不相关的对话。现在统一落到唯一的置顶会话"助理"，其它会话保持干净。
    """
    return record_assistant_message(app, role, content, images)


def broadcast_to_web_main(event_type: str, data: dict):
    """向"助理"会话的前端连接广播实时事件（流式文字、工具卡片、深度思考）。

    pet / QQ 主人的一次对话在 Web 上只有一处落点，就是"助理"。
    """
    target_sids = [ASSISTANT_SESSION_ID]
    clean_data = _sanitize_for_json(data)
    for sid in target_sids:
        try:
            with _active_turns_lock:
                turn = _active_session_turns.get(sid)
                if turn:
                    if event_type == "status":
                        turn["status"] = data.get("status") or data.get("message") or ""
                    elif event_type == "text_delta":
                        full = data.get("full", "")
                        block = _turn_start_block(sid, "text")
                        if block:
                            block["text"] = full
                        turn["text"] = full
                        turn["reasoning_done"] = True
                    elif event_type == "reasoning_delta":
                        full = data.get("full", "")
                        block = _turn_start_block(sid, "reasoning")
                        if block:
                            block["text"] = full
                        turn["reasoning"] = full
                        turn["reasoning_done"] = False
                    elif event_type == "reasoning_complete":
                        full = data.get("full", "")
                        turn["reasoning"] = full or turn.get("reasoning", "")
                        turn["reasoning_done"] = True
                        _turn_end_block(sid, "reasoning")
                    elif event_type == "tool_call":
                        tool_name = data.get("tool", "")
                        tool_args = data.get("args")
                        turn["reasoning_done"] = True
                        turn["text"] = ""
                        turn.setdefault("tools", []).append({"name": tool_name, "args": tool_args, "status": "running"})
                        block = _turn_start_block(sid, "tools")
                        if block is not None:
                            block.setdefault("items", []).append({"name": tool_name, "args": tool_args, "status": "running"})
                    elif event_type == "tool_result":
                        tool_name = data.get("tool", "")
                        result = data.get("result")
                        for t in reversed(turn.get("tools", [])):
                            if t["name"] == tool_name and t.get("status") == "running":
                                t["status"] = "done"
                                t["result"] = result
                                break
                        for block in reversed(turn.get("blocks", [])):
                            if block.get("type") != "tools":
                                continue
                            for item in reversed(block.get("items", [])):
                                if item["name"] == tool_name and item.get("status") == "running":
                                    item["status"] = "done"
                                    item["result"] = result
                                    break
                            else:
                                continue
                            break
                    elif event_type == "text_reset":
                        turn["text"] = ""
                        _turn_end_block(sid, "text")
            broadcast_sse(sid, event_type, clean_data)
        except Exception:
            pass


def broadcast_to_open_main_sessions(event_type: str, data: dict):
    """广播给当前打开的所有 web:main:* 会话（与具体会话无关的通用事件，例如后台任务结束）。

    跟渠道对话相关的实时事件走 broadcast_to_web_main，只落「助理」一处。
    """
    clean_data = _sanitize_for_json(data)
    for sid in get_active_web_main_sessions():
        try:
            broadcast_sse(sid, event_type, clean_data)
        except Exception:
            pass

def _init_task_manager_hooks():
    try:
        from core.task_manager import TaskManager
        def _on_task_finished(task):
            broadcast_to_open_main_sessions("task_finished", {
                "task_id": task.task_id,
                "name": task.name,
                "status": task.status,
                "exit_code": task.exit_code,
                "duration_sec": round((task.ended_at or time.time()) - task.started_at, 1),
                "output_preview": task.tail_output(max_chars=200),
            })
        TaskManager.get_instance().register_completion_callback(_on_task_finished)
    except Exception:
        pass

_init_task_manager_hooks()



class ConfigServerHandler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except OSError as exc:
            if getattr(exc, "winerror", None) in (10053, 10054):
                self.close_connection = True
            else:
                raise

    def _set_headers(self, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if getattr(self, "_set_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"minibot_session={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()

    def _authorize(self, write=False):
        host = self.headers.get("Host", "")
        valid = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if host not in valid:
            self._set_headers(403)
            self.wfile.write(b'{"error":"invalid host"}')
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in valid}:
            self._set_headers(403)
            self.wfile.write(b'{"error":"invalid origin"}')
            return False

        path_only = self.path.split("?")[0]
        if path_only in ("/", "/index.html", "/config", "/harness") and not write:
            self._set_cookie = True
            return True

        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        token = cookie.get("minibot_session")
        if not token or not secrets.compare_digest(
            token.value, self.server.session_token
        ):
            self._set_headers(403)
            self.wfile.write(b'{"error":"open configuration page first"}')
            return False

        # SSE streams do not send X-MiniBot-Request headers natively via EventSource
        if write and self.headers.get("X-MiniBot-Request") != "1" and not self.path.startswith("/api/stream"):
            self._set_headers(403)
            self.wfile.write(b'{"error":"missing request header"}')
            return False
        return True

    def do_OPTIONS(self):
        self._set_headers(403)

    def _get_app(self):
        app = getattr(self.server, "app", None)
        if app is None:
            # Lazy fallback singleton
            from main import MiniBotApp
            app = MiniBotApp(mode="local")
            self.server.app = app
        return app

    def do_GET(self):
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # 1. Main HTML
        if path in ("/", "/index.html", "/config", "/harness"):
            if HTML_FILE.exists():
                self._set_headers(200, "text/html; charset=utf-8")
                with open(HTML_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._set_headers(404, "text/plain")
                self.wfile.write(b"404 Not Found")
            return

        # Tasks API
        if path == "/api/tasks":
            from core.task_manager import TaskManager
            status_filter = params.get("status", [None])[0]
            try:
                limit_val = int(params.get("limit", [10])[0])
            except ValueError:
                limit_val = 10
            tasks = TaskManager.get_instance().list_tasks(status=status_filter, limit=limit_val)
            self._set_headers(200)
            self.wfile.write(json.dumps({"ok": True, "tasks": tasks}, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/api/tasks/detail":
            task_id = params.get("task_id", [""])[0]
            from core.task_manager import TaskManager
            detail = TaskManager.get_instance().check_task(task_id)
            self._set_headers(200 if detail.get("ok") else 404)
            self.wfile.write(json.dumps(detail, ensure_ascii=False).encode("utf-8"))
            return

        # 2. Workspaces list API
        if path == "/api/workspaces":
            ws = list_workspaces()
            self._set_headers(200)
            self.wfile.write(json.dumps({"success": True, "data": ws}, ensure_ascii=False).encode("utf-8"))
            return

        # 2b. Local folder browser API (read-only, directories only)
        if path == "/api/fs/browse":
            raw = (params.get("path", [""])[0] or "").strip()
            cur = Path(raw).expanduser() if raw else None
            dirs: List[Dict[str, str]] = []
            try:
                if cur is None or not cur.exists():
                    entries: List[Dict[str, str]] = []
                    if sys.platform == "win32":
                        import string
                        for letter in string.ascii_uppercase:
                            drive = Path(f"{letter}:/")
                            if drive.exists():
                                entries.append({"name": f"{letter}:\\", "path": str(drive)})
                    else:
                        cur = Path.home()
                    if cur is None or (raw == "" and sys.platform == "win32"):
                        self._set_headers(200)
                        self.wfile.write(json.dumps({
                            "success": True, "path": "", "parent": "", "home": str(Path.home()),
                            "dirs": entries,
                        }, ensure_ascii=False).encode("utf-8"))
                        return
                target = cur.resolve()
                for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                    try:
                        if child.is_dir() and not child.name.startswith("."):
                            dirs.append({"name": child.name, "path": str(child)})
                    except OSError:
                        continue
                parent = str(target.parent) if target.parent != target else ""
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "success": True, "path": str(target), "parent": parent,
                    "home": str(Path.home()), "dirs": dirs,
                }, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "success": False, "error": str(exc), "path": raw,
                    "parent": str(cur.parent) if cur else "", "dirs": [],
                }, ensure_ascii=False).encode("utf-8"))
            return

        # 3. Workspace files list API
        if path == "/api/workspace/files":
            scope = params.get("scope", ["main"])[0]
            subpath = params.get("subpath", [""])[0]
            files = list_workspace_files(scope=scope, subpath=subpath)
            self._set_headers(200)
            self.wfile.write(json.dumps({"success": True, "data": files, "scope": scope}, ensure_ascii=False).encode("utf-8"))
            return

        # 4. Workspace read file API (supports code, text and binary images)
        if path == "/api/workspace/file":
            scope = params.get("scope", ["main"])[0]
            rel_p = params.get("path", [""])[0]
            try:
                from core.workspace import resolve_file
                target = resolve_file(rel_p, scope)
                if not target.exists():
                    p_abs = Path(rel_p)
                    if p_abs.exists():
                        target = p_abs
                    else:
                        self._set_headers(404)
                        self.wfile.write(json.dumps({"success": False, "error": "文件不存在"}, ensure_ascii=False).encode("utf-8"))
                        return
                ext = target.suffix.lower()
                if ext in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'):
                    mime = 'image/' + ('jpeg' if ext in ('.jpg', '.jpeg') else ext.lstrip('.'))
                    self._set_headers(200, mime)
                    self.wfile.write(target.read_bytes())
                    return
                res = read_workspace_file(scope=scope, rel_path=rel_p)
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": True, "data": res}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self._set_headers(404)
                self.wfile.write(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        # 5. Sessions list API (grouped by workspace)
        if path == "/api/sessions":
            scope = params.get("scope", ["main"])[0]
            app = self._get_app()
            sessions = []
            # main 空间的"助理"会话先占位，保证它在列表里始终存在且排第一。
            try:
                ensure_assistant_session(app, scope)
            except Exception as e:
                print("Failed to ensure assistant session:", e)
            try:
                with app.memory._get_conn() as db:
                    rows = db.execute(
                        """
                        SELECT session_id, MAX(updated_at) as last_updated FROM (
                            SELECT session_id, updated_at FROM sessions_v2 WHERE session_id LIKE ?
                            UNION ALL
                            SELECT session_id, updated_at FROM task_states WHERE session_id LIKE ?
                            UNION ALL
                            SELECT session_id, created_at as updated_at FROM messages_v2 WHERE session_id LIKE ?
                        ) GROUP BY session_id ORDER BY last_updated DESC LIMIT 50
                        """,
                        (f"web:{scope}%", f"web:{scope}%", f"web:{scope}%")
                    ).fetchall()
                    for r in rows:
                        sid = r["session_id"]
                        state = app.memory.get_task_state(sid) or {}
                        title = state.get("original_goal", "").strip()
                        # Auto-rename if missing or placeholder title
                        if not title or title in ("新对话", "新任务对话", "默认会话", "未命名会话"):
                            msg_rows = db.execute(
                                "SELECT payload FROM messages_v2 WHERE session_id=? ORDER BY id ASC LIMIT 5",
                                (sid,)
                            ).fetchall()
                            for m in msg_rows:
                                try:
                                    p = json.loads(m["payload"])
                                    kind = str((p.get("meta") or {}).get("kind") or "")
                                    if kind in _INTERNAL_MESSAGE_KINDS:
                                        continue
                                    if p.get("role") == "user" and isinstance(p.get("content"), str) and p.get("content"):
                                        c = p["content"].strip().split("\n")[0].strip()
                                        if c:
                                            title = (c[:24] + "...") if len(c) > 24 else c
                                            state["original_goal"] = title
                                            app.memory.save_task_state(sid, state)
                                            break
                                except Exception:
                                    pass
                        if not title:
                            title = "默认会话" if sid.endswith(":default") else "新对话"
                        is_active = app.harness.is_session_active(sid)
                        sessions.append({
                            "session_id": sid,
                            "scope": scope,
                            "title": title[:40],
                            "is_active": is_active,
                            "status": state.get("status", "idle"),
                            "pinned": bool(state.get("pinned")) or sid == ASSISTANT_SESSION_ID,
                        })
            except Exception as e:
                print("Failed to query sessions:", e)

            # "助理"即使还没有任何消息，也要出现在列表最上方。
            if scope in ("main", "local") and not any(
                s["session_id"] == ASSISTANT_SESSION_ID for s in sessions
            ):
                sessions.insert(0, {
                    "session_id": ASSISTANT_SESSION_ID,
                    "scope": scope,
                    "title": ASSISTANT_TITLE,
                    "is_active": app.harness.is_session_active(ASSISTANT_SESSION_ID),
                    "status": "idle",
                    "pinned": True,
                })
            # 置顶项排最前，其余保持最近活跃顺序。
            sessions.sort(key=lambda s: 0 if s.get("pinned") else 1)

            if not sessions:
                default_sid = f"web:{scope}:default"
                is_active = app.harness.is_session_active(default_sid)
                sessions.append({
                    "session_id": default_sid,
                    "scope": scope,
                    "title": "默认会话",
                    "is_active": is_active,
                    "status": "idle"
                })
            self._set_headers(200)
            self.wfile.write(json.dumps({"success": True, "data": sessions}, ensure_ascii=False).encode("utf-8"))
            return

        # 6. Session messages history API (includes database message IDs for rollback)
        if path == "/api/session/messages":
            session_id = params.get("session_id", ["web:main:default"])[0]
            app = self._get_app()
            is_active = app.harness.is_session_active(session_id)
            task_state = app.memory.get_task_state(session_id) or {}
            messages_with_id = []
            try:
                import sqlite3
                with app.memory._get_conn() as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        """
                        SELECT id, payload, created_at FROM archived_messages_v2 WHERE session_id=?
                        UNION ALL
                        SELECT id, payload, created_at FROM messages_v2 WHERE session_id=?
                        ORDER BY id ASC
                        """,
                        (session_id, session_id)
                    ).fetchall()
                    for r in rows:
                        try:
                            p = json.loads(r["payload"])
                            p["_id"] = r["id"]
                            p["_created_at"] = r["created_at"]
                            messages_with_id.append(p)
                        except Exception:
                            pass
            except Exception:
                history, _ = app.memory.get_history(session_id)
                messages_with_id = history
            with _active_turns_lock:
                active_turn = _active_session_turns.get(session_id)
            messages_with_id = _drop_duplicated_steerings(messages_with_id, active_turn)
            messages_with_id = _filter_display_messages(messages_with_id)
            self._set_headers(200)
            self.wfile.write(safe_json_dumps({
                "success": True,
                "session_id": session_id,
                "is_active": is_active,
                "task_state": task_state,
                "data": messages_with_id,
                "active_turn": active_turn
            }).encode("utf-8"))
            return

        # Session file changes list API
        if path == "/api/session/changes":
            session_id = params.get("session_id", [""])[0]
            from core.checkpoint import get_checkpoint_manager
            changes = get_checkpoint_manager().get_session_changes(session_id)
            self._set_headers(200)
            self.wfile.write(safe_json_dumps({"success": True, "data": changes}).encode("utf-8"))
            return

        # 7. Server-Sent Events (SSE) streaming API
        if path == "/api/stream":
            self.close_connection = True
            try:
                self.request.settimeout(None)
            except Exception:
                pass
            session_id = params.get("session_id", ["web:main:default"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            q = register_sse_subscriber(session_id)
            # Push initial keep-alive
            try:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
            except Exception:
                unregister_sse_subscriber(session_id, q)
                return

            app = self._get_app()
            with _active_turns_lock:
                active_turn = _active_session_turns.get(session_id)
            if active_turn:
                try:
                    payload_str = safe_json_dumps(active_turn)
                    self.wfile.write(f"event: active_snapshot\ndata: {payload_str}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass
            elif app and app.harness.is_session_active(session_id):
                try:
                    self.wfile.write(f"event: status\ndata: {safe_json_dumps({'status': '正在处理中...'})}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass

            try:
                while True:
                    try:
                        msg = q.get(timeout=10.0)
                        event = msg.get("event", "message")
                        data_str = safe_json_dumps(msg.get("data", {}))
                        chunk = f"event: {event}\ndata: {data_str}\n\n".encode("utf-8")
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except queue.Empty:
                        # Keep-alive heartbeat comment every 10s
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass
            finally:
                unregister_sse_subscriber(session_id, q)
            return

        # Media preview API
        if path == "/api/media/preview":
            ref = params.get("ref", [""])[0]
            scope = params.get("scope", ["main"])[0]
            if not ref:
                self._set_headers(400)
                return
            try:
                from core.media_store import MediaStore
                ms = MediaStore()
                manifest, data = None, None
                for s in (scope, "main", "local"):
                    try:
                        manifest, data = ms.get(ref, s)
                        if data:
                            break
                    except Exception:
                        pass
                if not data:
                    self._set_headers(404, "text/plain")
                    self.wfile.write(b"Media not found")
                    return
                mime = manifest.get("mime", "image/png") if manifest else "image/png"
                total = len(data)
                # 视频/音频需要 Range 才能拖动进度条，普通图片走完整响应即可。
                start, end, partial = 0, total - 1, False
                range_header = (self.headers.get("Range") or "").strip()
                if range_header.startswith("bytes=") and total:
                    spec = range_header[6:].split(",")[0].strip()
                    first, _, last = spec.partition("-")
                    try:
                        if first:
                            start = max(0, int(first))
                            if last:
                                end = min(int(last), total - 1)
                        elif last:
                            start = max(0, total - int(last))
                    except ValueError:
                        start, end = 0, total - 1
                    if start > end or start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.end_headers()
                        return
                    partial = True
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                self.wfile.write(data[start:end + 1] if total else b"")
            except Exception as exc:
                self._set_headers(500, "text/plain")
                self.wfile.write(str(exc).encode("utf-8"))
            return

        # Available VLM models API
        if path == "/api/models":
            from core.config import get_vlm_models
            models = get_vlm_models()
            safe_models = [
                {
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "model": m.get("model"),
                    "enable_thinking": m.get("enable_thinking", False),
                    "is_default": m.get("is_default", False),
                }
                for m in models
            ]
            default_id = next((m["id"] for m in safe_models if m.get("is_default")), safe_models[0]["id"] if safe_models else "")
            self._set_headers(200)
            self.wfile.write(json.dumps({"models": safe_models, "default": default_id}, ensure_ascii=False).encode("utf-8"))
            return

        # 8. Existing Config API
        if path == "/api/config":
            cfg = load_config()
            for section, field in [
                ("vlm", "api_key"),
                ("jev", "api_key"),
                ("anysearch", "api_key"),
                ("stt", "api_key"),
                ("image", "api_key"),
            ]:
                cfg[section][field + "_configured"] = bool(cfg[section].get(field))
                cfg[section][field] = ""
            if isinstance(cfg.get("vlm", {}).get("models"), list):
                for m in cfg["vlm"]["models"]:
                    if isinstance(m, dict):
                        m["api_key_configured"] = bool(m.get("api_key"))
                        m["api_key"] = ""
            cfg["channels"]["napcat"]["access_token_configured"] = bool(
                cfg["channels"]["napcat"].get("access_token")
            )
            cfg["channels"]["napcat"]["access_token"] = ""
            self._set_headers(200)
            self.wfile.write(json.dumps(cfg, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/api/stt/local/status":
            from core import local_stt

            try:
                payload = {"success": True, "data": local_stt.status()}
            except Exception as exc:
                payload = {"success": False, "error": str(exc)[:300]}
            self._set_headers(200)
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        self._set_headers(404)
        self.wfile.write(b'{"error": "Not Found"}')

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._set_headers(400)
            return
        if not 0 <= content_length <= 20971520:  # 20MB max for image uploads
            self._set_headers(413)
            return
        body = self.rfile.read(content_length) if content_length > 0 else b""

        if not self._authorize(write=True):
            return

        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._set_headers(400)
            self.wfile.write(b'{"error":"invalid JSON"}')
            return
        if not isinstance(req_data, dict):
            self._set_headers(400)
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/tasks/cancel":
            task_id = req_data.get("task_id", "")
            from core.task_manager import TaskManager
            res = TaskManager.get_instance().cancel_task(task_id)
            self._set_headers(200)
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        # 1. Create Workspace API
        if path == "/api/workspaces":
            name = req_data.get("name", "").strip()
            if not name:
                self._set_headers(400)
                self.wfile.write(b'{"error": "Workspace name cannot be empty"}')
                return
            folders = req_data.get("folders") or []
            ws = create_workspace(name, folders if isinstance(folders, list) else None)
            self._set_headers(200)
            self.wfile.write(json.dumps({"success": True, "data": ws}, ensure_ascii=False).encode("utf-8"))
            return

        # 2. Create Session API
        if path == "/api/sessions":
            scope = req_data.get("scope", "main")
            title = req_data.get("title", "").strip()
            sid = f"web:{scope}:{secrets.token_hex(4)}"
            app = self._get_app()
            now = time.time()
            app.memory.save_task_state(sid, {
                "original_goal": title or "新对话",
                "status": "awaiting_input",
                "scope": scope
            })
            try:
                with app.memory._get_conn() as db:
                    db.execute("INSERT OR REPLACE INTO sessions_v2 VALUES (?,?)", (sid, now))
            except Exception:
                pass
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "success": True,
                "data": {
                    "session_id": sid,
                    "scope": scope,
                    "title": title or "新对话",
                    "is_active": False,
                    "status": "awaiting_input"
                }
            }, ensure_ascii=False).encode("utf-8"))
            return

        # Rename Session API
        if path == "/api/sessions/rename":
            session_id = req_data.get("session_id", "").strip()
            title = req_data.get("title", "").strip()
            if not session_id or not title:
                self._set_headers(400)
                self.wfile.write(b'{"error": "session_id and title required"}')
                return
            if session_id == ASSISTANT_SESSION_ID:
                # 名称被程序当作汇总会话的标识，改名会在下次刷新时被复位，直接拒绝更清楚。
                self._set_headers(400)
                self.wfile.write(json.dumps(
                    {"success": False, "error": "「助理」是置顶汇总会话，不支持重命名"},
                    ensure_ascii=False).encode("utf-8"))
                return
            app = self._get_app()
            state = app.memory.get_task_state(session_id) or {}
            state["original_goal"] = title
            app.memory.save_task_state(session_id, state)
            try:
                with app.memory._get_conn() as db:
                    db.execute("INSERT OR REPLACE INTO sessions_v2 VALUES (?,?)", (session_id, time.time()))
            except Exception:
                pass
            broadcast_sse(session_id, "session_renamed", {"session_id": session_id, "title": title})
            self._set_headers(200)
            self.wfile.write(json.dumps({"success": True, "title": title}, ensure_ascii=False).encode("utf-8"))
            return

        # Delete Session API
        if path == "/api/sessions/delete":
            session_id = req_data.get("session_id", "").strip()
            if not session_id:
                self._set_headers(400)
                self.wfile.write(b'{"error": "session_id required"}')
                return
            if session_id == ASSISTANT_SESSION_ID:
                # 删掉也会在下次刷新时重建，与其留下"删了又回来"的困惑，不如明确拒绝。
                self._set_headers(400)
                self.wfile.write(json.dumps(
                    {"success": False, "error": "「助理」是置顶汇总会话，不支持删除；如需清空请用清空会话"},
                    ensure_ascii=False).encode("utf-8"))
                return
            app = self._get_app()
            try:
                with app.memory._get_conn() as db:
                    for table in ("messages_v2", "archived_messages_v2", "summaries_v2", "sessions_v2", "task_states"):
                        try:
                            db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
                        except Exception:
                            pass
            except Exception:
                pass
            self._set_headers(200)
            self.wfile.write(b'{"success": true}')
            return

        # Manual Compact Session API (/cc command)
        if path == "/api/session/compact":
            session_id = req_data.get("session_id", "").strip()
            if not session_id:
                self._set_headers(400)
                self.wfile.write(b'{"error": "session_id required"}')
                return
            app = self._get_app()
            if app.harness.is_session_active(session_id):
                self._set_headers(400)
                self.wfile.write(json.dumps({"success": False, "error": "当前任务正在执行中，请在任务完成后再压缩上下文"}, ensure_ascii=False).encode("utf-8"))
                return
            history, _ = app.memory.get_history(session_id)
            if not history or len(history) < 2:
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": "当前会话历史过短，暂无需压缩"}, ensure_ascii=False).encode("utf-8"))
                return
            from core.compaction import ContextCompactor
            compactor = ContextCompactor(brain=app.brain, memory=app.memory, session_id=session_id)
            old_cutoff = compactor.summary["covered_until_id"] if compactor.summary else 0
            ok = compactor._run_compaction_worker(history, keep=6)
            new_cutoff = compactor.summary["covered_until_id"] if compactor.summary else 0
            if ok or new_cutoff > old_cutoff:
                broadcast_sse(session_id, "session_compacted", {
                    "session_id": session_id,
                    "covered_until_id": new_cutoff,
                    "summary": compactor.summary.get("summary", "") if compactor.summary else ""
                })
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "success": True,
                    "message": "上下文压缩完成",
                    "covered_until_id": new_cutoff,
                    "summary": compactor.summary.get("summary", "") if compactor.summary else ""
                }, ensure_ascii=False).encode("utf-8"))
            else:
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": "未达到压缩阈值或无多余上下文需要压缩"
                }, ensure_ascii=False).encode("utf-8"))
            return

        # Clear Session API (/clear command)
        if path == "/api/session/clear":
            session_id = req_data.get("session_id", "").strip()
            if not session_id:
                self._set_headers(400)
                self.wfile.write(b'{"error": "session_id required"}')
                return
            app = self._get_app()
            if app.harness.is_session_active(session_id):
                self._set_headers(400)
                self.wfile.write(json.dumps({"success": False, "error": "当前任务正在执行中，请先停止任务后再清空"}, ensure_ascii=False).encode("utf-8"))
                return
            scope = session_id.split(":")[1] if ":" in session_id else "main"
            app.memory.clear_session(session_id)
            if session_id.startswith("web:main") or session_id in ("local:pet", "web:main"):
                app.memory.clear_session("local:pet")
                app.memory.clear_session("web:main")
                # 「助理」汇总的是各渠道的对话，清空 main 空间时一起清。
                app.memory.clear_session(ASSISTANT_SESSION_ID)
                try:
                    owner_ids = load_config()["channels"]["napcat"].get("owner_ids", [])
                    for oid in owner_ids:
                        app.memory.clear_session(f"qq:private:{oid}")
                except Exception:
                    pass
            try:
                from core.checkpoint import get_checkpoint_manager
                get_checkpoint_manager().clear_session(session_id)
            except Exception:
                pass
            if session_id == ASSISTANT_SESSION_ID:
                # 置顶会话清空后要立刻把名字与置顶标记写回去，
                # 否则下次刷新会退化成一条普通会话。
                ensure_assistant_session(app)
            else:
                app.memory.save_task_state(session_id, {
                    "original_goal": "新对话",
                    "status": "awaiting_input",
                    "scope": scope
                })
            _turn_reset(session_id)
            broadcast_sse(session_id, "session_cleared", {"session_id": session_id})
            self._set_headers(200)
            self.wfile.write(json.dumps({"success": True, "message": "当前会话历史已清空"}, ensure_ascii=False).encode("utf-8"))
            return

        # Rollback session files and history to a specific message point
        if path == "/api/session/rollback":
            session_id = req_data.get("session_id", "")
            message_id = req_data.get("message_id")
            if not session_id or message_id is None:
                self._set_headers(400)
                self.wfile.write(b'{"error": "session_id and message_id required"}')
                return
            app = self._get_app()
            scope = session_id.split(":")[1] if ":" in session_id else session_id
            from core.checkpoint import get_checkpoint_manager
            res = get_checkpoint_manager().rollback_to_message(
                session_id, int(message_id), app.memory, scope=scope
            )
            self._set_headers(200)
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        # Revert changes made in the latest turn
        if path == "/api/turn/revert":
            session_id = req_data.get("session_id", "")
            if not session_id:
                self._set_headers(400)
                self.wfile.write(b'{"error": "session_id required"}')
                return
            from core.checkpoint import get_checkpoint_manager
            res = get_checkpoint_manager().revert_latest_turn(session_id)
            self._set_headers(200)
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        # Open file in OS default application API
        if path == "/api/workspace/open":
            scope = req_data.get("scope", "main")
            rel_p = req_data.get("path", "").strip()
            if not rel_p:
                self._set_headers(400)
                self.wfile.write(b'{"error": "path cannot be empty"}')
                return
            from core.workspace import resolve_file
            try:
                target = resolve_file(rel_p, scope)
                if not target.exists():
                    p_abs = Path(rel_p)
                    if p_abs.exists():
                        target = p_abs
                    else:
                        self._set_headers(404)
                        self.wfile.write(json.dumps({"success": False, "error": f"文件不存在: {rel_p}"}, ensure_ascii=False).encode("utf-8"))
                        return
                if sys.platform == "win32":
                    os.startfile(str(target))
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(target)], check=False)
                else:
                    subprocess.run(["xdg-open", str(target)], check=False)
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": True, "path": str(target)}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self._set_headers(500)
                self.wfile.write(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        # 3. Chat & Steering Task Submission API
        if path == "/api/chat":
            session_id = req_data.get("session_id", "web:main:default")
            scope = req_data.get("scope", "main")
            prompt = req_data.get("prompt", "").strip()
            mode = req_data.get("mode", "auto")  # 'auto' or 'steer'

            input_images = req_data.get("input_images", [])
            if not prompt and not input_images:
                self._set_headers(400)
                self.wfile.write(b'{"error": "prompt or input_images cannot be empty"}')
                return

            if not prompt and input_images:
                prompt = "请查看上传的图片"

            # Register base64 images into MediaStore
            from core.media_store import MediaStore
            ms = MediaStore()
            resolved_images = []
            for img in input_images:
                if isinstance(img, str) and img.startswith("data:image/"):
                    try:
                        header, b64data = img.split(",", 1)
                        raw = base64.b64decode(b64data)
                        asset = ms.add(raw, scope=scope, origin="user")
                        resolved_images.append(ms.data_url(asset["media_ref"], scope))
                    except Exception as exc:
                        print("Failed to register image into MediaStore:", exc)
                        resolved_images.append(img)
                else:
                    resolved_images.append(img)

            app = self._get_app()
            is_running = app.harness.is_session_active(session_id)

            # Explicit steer or mid-run steering
            if mode == "steer" or is_running:
                # 检查第一层 Jev 判定
                task_state = app.memory.get_task_state(session_id) or {}
                task_state["is_running"] = is_running
                intent = "steering" if mode == "steer" else app.jev.classify_steering_intent(prompt, task_state=task_state)

                if intent == "steering" and is_running:
                    # Inject steering mid-run into active Harness!
                    ok = app.harness.inject_steering(session_id, prompt)
                    broadcast_sse(session_id, "steering", {"text": prompt, "success": ok})
                    self._set_headers(200)
                    self.wfile.write(json.dumps({
                        "success": ok,
                        "steered": True,
                        "message": "实时引导已注入活动任务，正在调整方向！" if ok else "引导注入失败"
                    }, ensure_ascii=False).encode("utf-8"))
                    return

            # Auto-rename session if it has default placeholder title
            state = app.memory.get_task_state(session_id) or {}
            orig_goal = state.get("original_goal", "").strip()
            is_placeholder = (not orig_goal) or orig_goal in ("新对话", "新任务对话", "默认会话", "未命名会话")
            current_title = orig_goal
            if is_placeholder and prompt:
                first_line = prompt.strip().split("\n")[0].strip()
                current_title = (first_line[:24] + "...") if len(first_line) > 24 else first_line
                state["original_goal"] = current_title
                state["scope"] = scope
                app.memory.save_task_state(session_id, state)
                try:
                    with app.memory._get_conn() as db:
                        db.execute("INSERT OR REPLACE INTO sessions_v2 VALUES (?,?)", (session_id, time.time()))
                except Exception:
                    pass
                broadcast_sse(session_id, "session_renamed", {"session_id": session_id, "title": current_title})

            # Record user message in persistent memory immediately so it's queryable by /api/session/messages
            saved_msg = app.memory.append_message(session_id, {
                "role": "user",
                "content": prompt,
                "images": resolved_images if resolved_images else []
            })

            # Broadcast user message to SSE
            broadcast_sse(session_id, "user_message", {
                "id": saved_msg.get("_id"),
                "content": prompt,
                "session_id": session_id,
                "images": input_images
            })

            selected_model = req_data.get("model", "").strip() or None
            thinking_effort = req_data.get("thinking_effort")
            if thinking_effort is not None:
                thinking_effort = str(thinking_effort).strip()

            reasoning_chunks = []
            reasoning_done = [False]
            current_reasoning = [""]

            with _active_turns_lock:
                _active_session_turns[session_id] = {
                    "session_id": session_id,
                    "scope": scope,
                    "status": "正在深度思考与规划...",
                    "reasoning": "",
                    "reasoning_done": False,
                    "text": "",
                    "tools": [],
                    # blocks 是"按发生顺序"排列的段列表：思考 / 正文 / 工具批次。
                    # 前端据此渲染，思考块不再被强行钉在消息最上方。
                    "blocks": [],
                    "started_at": time.time(),
                    "is_active": True
                }
                _active_turn_cursors[session_id] = {}

            # 上游"首个内容"信号：思考/正文/工具任一到达即置位。
            # 在它置位之前，界面只会看到一个在走的秒表，看起来就像卡死；
            # 所以这里起一个心跳线程，把"我在等上游模型"这件事明确说出来。
            first_content = threading.Event()

            def upstream_heartbeat():
                waited = 0
                marks = {10, 20, 30, 45, 60, 90, 120, 150, 180, 240, 300}
                while not first_content.wait(5):
                    waited += 5
                    with _active_turns_lock:
                        alive = _active_session_turns.get(session_id) is not None
                    if not alive:
                        return
                    if waited not in marks:
                        continue
                    if waited < 60:
                        msg = f"等待上游模型返回首个内容…（已等待 {waited}s）"
                    else:
                        msg = f"上游模型已 {waited}s 未返回任何内容，可点「停止」中断或检查模型接口"
                    with _active_turns_lock:
                        turn = _active_session_turns.get(session_id)
                        if turn:
                            turn["status"] = msg
                    broadcast_sse(session_id, "status", {"status": msg})

            threading.Thread(
                target=upstream_heartbeat, daemon=True, name="upstream-heartbeat"
            ).start()

            def on_reasoning_chunk(delta, full):
                first_content.set()
                reasoning_chunks.append(delta)
                current_reasoning[0] = full
                reasoning_done[0] = False
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        block = _turn_start_block(session_id, "reasoning")
                        if block:
                            block["text"] = full
                        turn["reasoning"] = full
                        turn["reasoning_done"] = False
                broadcast_sse(session_id, "reasoning_delta", {"delta": delta, "full": full})

            def on_reasoning_complete(full):
                reasoning_done[0] = True
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        turn["reasoning"] = full or current_reasoning[0]
                        turn["reasoning_done"] = True
                    _turn_end_block(session_id, "reasoning")
                broadcast_sse(session_id, "reasoning_complete", {"full": full or current_reasoning[0]})

            # Define SSE streaming callbacks
            def on_text_chunk(delta, full):
                first_content.set()
                if current_reasoning[0] and not reasoning_done[0]:
                    on_reasoning_complete(current_reasoning[0])
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        block = _turn_start_block(session_id, "text")
                        if block:
                            block["text"] = full
                        turn["text"] = full
                        turn["reasoning_done"] = True
                broadcast_sse(session_id, "text_delta", {"delta": delta, "full": full})

            def on_text_reset(reason=""):
                # 本轮草稿被作废（核验重试/连接重试/引导打断），让界面不要把新旧文本混在一起。
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        turn["text"] = ""
                        cursor = _active_turn_cursors.get(session_id) or {}
                        block = cursor.get("text")
                        if block is not None:
                            block["discarded"] = reason or True
                    _turn_end_block(session_id, "text")
                broadcast_sse(session_id, "text_reset", {"reason": reason or ""})

            def on_status_change(status_msg):
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        turn["status"] = status_msg
                broadcast_sse(session_id, "status", {"status": status_msg})

            def on_tool_call(tool_name, tool_args):
                first_content.set()
                clean_args = _sanitize_for_json(tool_args)
                if current_reasoning[0] and not reasoning_done[0]:
                    on_reasoning_complete(current_reasoning[0])
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        turn["reasoning_done"] = True
                        turn["text"] = ""
                        turn["tools"].append({"name": tool_name, "args": clean_args, "status": "running"})
                        block = _turn_start_block(session_id, "tools")
                        if block is not None:
                            block["items"].append({"name": tool_name, "args": clean_args, "status": "running"})
                broadcast_sse(session_id, "tool_call", {"tool": tool_name, "args": clean_args})

            def on_tool_result(tool_name, result):
                clean_result = _sanitize_for_json(result)
                with _active_turns_lock:
                    turn = _active_session_turns.get(session_id)
                    if turn:
                        for t in reversed(turn["tools"]):
                            if t["name"] == tool_name and t.get("status") == "running":
                                t["status"] = "done"
                                t["result"] = clean_result
                                break
                        for block in reversed(turn.get("blocks", [])):
                            if block.get("type") != "tools":
                                continue
                            for item in reversed(block.get("items", [])):
                                if item["name"] == tool_name and item.get("status") == "running":
                                    item["status"] = "done"
                                    item["result"] = clean_result
                                    break
                            else:
                                continue
                            break
                broadcast_sse(session_id, "tool_result", {"tool": tool_name, "result": clean_result})

            def on_steering_applied(steer_text):
                # 引导真正进入历史的位置就是它生效的时刻，按块记下来，
                # 刷新页面时才能插回原来的时序，而不是堆在整轮末尾。
                with _active_turns_lock:
                    block = _turn_start_block(session_id, "steering")
                    if block:
                        block["text"] = steer_text
                    _turn_end_block(session_id, "steering")
                broadcast_sse(session_id, "steering_applied", {"text": steer_text})

            def on_tool_delta(delta_info):
                first_content.set()
                clean_delta = _sanitize_for_json(delta_info)
                if current_reasoning[0] and not reasoning_done[0]:
                    on_reasoning_complete(current_reasoning[0])
                broadcast_sse(session_id, "tool_delta", clean_delta)

            # Submit to Dispatcher
            future = app.submit_query(
                prompt,
                custom_scope=scope,
                custom_session=session_id,
                input_images=resolved_images,
                on_text_chunk=on_text_chunk,
                on_status_change=on_status_change,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                on_steering_applied=on_steering_applied,
                on_tool_delta=on_tool_delta,
                on_reasoning_chunk=on_reasoning_chunk,
                on_reasoning_complete=on_reasoning_complete,
                on_text_reset=on_text_reset,
                model=selected_model,
                thinking_effort=thinking_effort,
                user_message_id=saved_msg.get("_id"),
            )

            # Background thread to await completion and broadcast complete event
            def await_reply(fut, sid):
                try:
                    reply = fut.result()
                    with _active_turns_lock:
                        stopped = _cancel_requests.pop(sid, None) is not None
                    if current_reasoning[0] and not reasoning_done[0]:
                        on_reasoning_complete(current_reasoning[0])
                    if stopped or (reply or "").strip() == "任务已取消。":
                        # 用户主动停止：不要再把收尾结果当成一次正常回答渲染。
                        broadcast_sse(sid, "cancelled", {"count": 0})
                        return
                    state = app.memory.get_task_state(sid) or {}
                    artifacts = state.get("artifacts", [])
                    file_changes = state.get("file_changes", [])
                    broadcast_sse(sid, "complete", {
                        "reply": reply,
                        "artifacts": artifacts,
                        "file_changes": file_changes
                    })
                except Exception as exc:
                    with _active_turns_lock:
                        stopped = _cancel_requests.pop(sid, None) is not None
                    if stopped:
                        broadcast_sse(sid, "cancelled", {"count": 0})
                    else:
                        broadcast_sse(sid, "error", {"error": str(exc)})
                finally:
                    first_content.set()
                    _turn_reset(sid)

            threading.Thread(target=await_reply, args=(future, session_id), daemon=True).start()

            self._set_headers(200)
            self.wfile.write(json.dumps({
                "success": True,
                "session_id": session_id,
                "title": current_title,
                "status": "running"
            }, ensure_ascii=False).encode("utf-8"))
            return

        # 4. Explicit Steering API
        if path == "/api/steer":
            session_id = req_data.get("session_id", "web:main:default")
            text = req_data.get("text", "").strip()
            if not text:
                self._set_headers(400)
                self.wfile.write(b'{"error": "steering text cannot be empty"}')
                return
            app = self._get_app()
            ok = app.harness.inject_steering(session_id, text)
            if ok:
                broadcast_sse(session_id, "steering", {"text": text, "success": True})
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "success": ok,
                "message": "实时引导已发送" if ok else "当前会话没有正在运行的任务可引导"
            }, ensure_ascii=False).encode("utf-8"))
            return

        # 5. Cancel Task API
        if path == "/api/cancel":
            session_id = req_data.get("session_id", "web:main:default")
            app = self._get_app()
            # 先打标记，再触发取消：这样即便模型已经收尾，等待线程也不会把结果当正常回答广播。
            with _active_turns_lock:
                _cancel_requests[session_id] = time.time()
            stopped = app.harness.cancel_session(session_id)
            count = app.dispatcher.cancel_pending(session_id)
            if not stopped and not count:
                with _active_turns_lock:
                    _cancel_requests.pop(session_id, None)
            _turn_reset(session_id)
            broadcast_sse(session_id, "cancelled", {"count": count, "stopped": bool(stopped)})
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "success": True,
                "stopped": bool(stopped),
                "count": count,
                "message": f"已停止当前任务及 {count} 条排队请求" if (stopped or count) else "当前没有正在运行的任务"
            }, ensure_ascii=False).encode("utf-8"))
            return

        # 6. Existing Config Save API
        if path == "/api/config":
            try:
                existing_cfg = load_config()
                existing_model_keys = {}
                for m in existing_cfg.get("vlm", {}).get("models", []):
                    if isinstance(m, dict):
                        mid = m.get("id") or m.get("model")
                        if mid and m.get("api_key"):
                            existing_model_keys[mid] = m.get("api_key")

                req_models = req_data.get("vlm", {}).get("models")
                if isinstance(req_models, list):
                    for m in req_models:
                        if isinstance(m, dict):
                            mid = m.get("id") or m.get("model")
                            if not m.get("api_key") and mid in existing_model_keys and not m.pop("clear_api_key", False):
                                m["api_key"] = existing_model_keys[mid]
                            m.pop("api_key_configured", None)

                for section in ("vlm", "jev", "anysearch", "stt", "image"):
                    values = req_data.get(section, {})
                    if values.pop("clear_api_key", False):
                        values["api_key"] = ""
                    elif not values.get("api_key"):
                        values.pop("api_key", None)
                    values.pop("api_key_configured", None)
                nap = req_data.get("channels", {}).get("napcat", {})
                if nap.pop("clear_access_token", False):
                    nap["access_token"] = ""
                elif not nap.get("access_token"):
                    nap.pop("access_token", None)
                nap.pop("access_token_configured", None)
                save_config(req_data)
            except (ValueError, TypeError, OSError, AttributeError) as exc:
                self._set_headers(400)
                self.wfile.write(
                    json.dumps({"success": False, "error": str(exc)}).encode()
                )
                return
            self._set_headers(200)
            self.wfile.write(b'{"success": true}')
            return

        # 6.5 本地 STT 安装/下载 与 转写连通性测试
        if path in ("/api/stt/local/install", "/api/stt/local/model", "/api/test/stt"):
            try:
                if path == "/api/stt/local/install":
                    from core import local_stt

                    payload = {
                        "success": True,
                        "data": local_stt.start_install(req_data.get("pip_index")),
                    }
                elif path == "/api/stt/local/model":
                    from core import local_stt

                    payload = {
                        "success": True,
                        "data": local_stt.start_download(req_data.get("model")),
                    }
                else:
                    from core.stt import test_connection

                    result = test_connection(
                        {
                            "mode": req_data.get("mode"),
                            "format": req_data.get("format"),
                            "base_url": req_data.get("base_url"),
                            "model": req_data.get("model"),
                            "language": req_data.get("language"),
                            "api_key": req_data.get("api_key"),
                        }
                    )
                    payload = {"success": True, "data": result}
            except Exception as exc:
                from core.console import clean

                payload = {"success": False, "error": clean(str(exc))}
            self._set_headers(200)
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        # 7. Existing Test APIs
        elif path in (
            "/api/anysearch/register",
            "/api/test/anysearch",
            "/api/ego/status",
            "/api/ego/install",
        ):
            try:
                if path == "/api/anysearch/register":
                    import re
                    from core.default_skills import anysearch_request

                    email = req_data.get("email", "").strip()
                    if (
                        not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
                        or len(email) > 254
                    ):
                        raise ValueError("请输入真实且可接收邮件的邮箱")
                    response = anysearch_request(
                        "/v1/auth/email/register", {"email": email}, key=""
                    )
                    account = response.get("data", {})
                    key = account.get("api_key", {}).get("key")
                    if not isinstance(key, str) or not key:
                        raise ValueError(
                            "注册未返回 API Key，请在 https://www.anysearch.com/login 登录后创建；不要重复注册"
                        )
                    save_config({"anysearch": {"api_key": key}})
                    result = {
                        "success": True,
                        "username": account.get("username", email),
                        "login_url": account.get(
                            "login_url", "https://www.anysearch.com/login"
                        ),
                    }
                elif path == "/api/test/anysearch":
                    from core.default_skills import anysearch_request

                    key = (
                        req_data.get("api_key", "").strip()
                        or load_config()["anysearch"]["api_key"]
                        or os.environ.get("ANYSEARCH_API_KEY", "")
                    )
                    response = anysearch_request(
                        "/v1/search", {"query": "AnySearch", "max_results": 1}, key=key
                    )
                    result = {
                        "success": True,
                        "mode": "key" if key else "anonymous",
                        "results": len((response.get("data") or {}).get("results", [])),
                    }
                elif path == "/api/ego/install":
                    from core.ego import start_install

                    result = {"success": True, "data": start_install()}
                else:
                    from core.ego import status

                    result = {"success": True, "data": status()}
            except Exception as exc:
                from core.console import clean

                result = {"success": False, "error": clean(str(exc))}
            self._set_headers(200)
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            return

        elif path == "/api/test/jev":
            base_url = req_data.get("base_url", "").rstrip("/")
            api_key = req_data.get("api_key", "").strip()
            saved = load_config()["jev"]
            if not api_key and base_url == saved["base_url"].rstrip("/"):
                api_key = saved.get("api_key", "")
            model = req_data.get("model", "vllm-sr/d3-turbo")

            if not api_key:
                self._set_headers(200)
                self.wfile.write(
                    json.dumps({"success": False, "error": "API Key 不能为空"}).encode(
                        "utf-8"
                    )
                )
                return

            from core.jev import JevClient

            start_t = time.monotonic()
            try:
                result = JevClient(base_url, api_key, model).decide_raw(
                    "System health probe: the system is responsive.",
                    {
                        "probe": {
                            "type": "noul",
                            "instructions": "Is the system responsive?",
                        }
                    },
                )
                probe = result["answers"]["probe"].get("noul")
                if not isinstance(probe, (float, int)) or not 0 <= probe <= 1:
                    raise ValueError("决策接口未返回有效 noul")
                body = {
                    "success": True,
                    "latency_ms": round((time.monotonic() - start_t) * 1000),
                    "data": result,
                }
            except Exception as exc:
                body = {
                    "success": False,
                    "error": str(exc).replace(api_key, "[redacted]"),
                }
            self._set_headers(200)
            self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))
            return

        elif path == "/api/test/vlm":
            base_url = req_data.get("base_url", "").rstrip("/")
            api_key = req_data.get("api_key", "").strip()
            saved = load_config()["vlm"]
            if not base_url:
                base_url = saved.get("base_url", "").rstrip("/")
            if not api_key:
                model_id = req_data.get("id") or req_data.get("model")
                found_key = ""
                for m in saved.get("models", []):
                    if isinstance(m, dict) and (m.get("id") == model_id or m.get("model") == model_id) and m.get("api_key"):
                        found_key = m.get("api_key")
                        break
                api_key = found_key or saved.get("api_key", "")
            model = req_data.get("model") or saved.get("model", "gpt-4o")

            if not api_key:
                self._set_headers(200)
                self.wfile.write(
                    json.dumps({"success": False, "error": "API Key 不能为空"}).encode(
                        "utf-8"
                    )
                )
                return

            endpoint = (
                f"{base_url}/chat/completions"
                if not base_url.endswith("/chat/completions")
                else base_url
            )
            payload = json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "MiniBot-Client/1.0",
                },
                method="POST",
            )

            start_t = time.time()
            try:
                with urlopen(req, timeout=10):
                    latency_ms = int((time.time() - start_t) * 1000)
                    self._set_headers(200)
                    self.wfile.write(
                        json.dumps({"success": True, "latency_ms": latency_ms}).encode(
                            "utf-8"
                        )
                    )
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                self._set_headers(200)
                self.wfile.write(
                    json.dumps(
                        {"success": False, "error": f"HTTP {e.code}: {err_text[:120]}"}
                    ).encode("utf-8")
                )
            except Exception as e:
                self._set_headers(200)
                self.wfile.write(
                    json.dumps({"success": False, "error": str(e)}).encode("utf-8")
                )
            return

        self._set_headers(404)
        self.wfile.write(b'{"error": "Unknown API endpoint"}')

    def log_message(self, format, *args):
        # Mute standard HTTP logging to console
        return


_servers = {}
_server_lock = threading.Lock()


class ConfigHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, app=None, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)
        self.session_token = secrets.token_urlsafe(32)
        self.app = app

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        request.settimeout(60)
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) or (
            isinstance(exc_val, OSError) and getattr(exc_val, "winerror", None) in (10053, 10054)
        ):
            return
        super().handle_error(request, client_address)

    def server_close(self):
        super().server_close()
        with _server_lock:
            _servers.pop(self.server_port, None)


def run_server(port=18080, open_browser=False, blocking=True, app=None):
    with _server_lock:
        server = _servers.get(port)
        if server is None:
            server = ConfigHTTPServer(("127.0.0.1", port), ConfigServerHandler, app=app)
            _servers[server.server_port] = server
            thread = threading.Thread(
                target=server.serve_forever, daemon=True, name="bot-webui-server"
            )
            thread.start()
        elif app is not None:
            server.app = app
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{server.server_port}")
    if blocking:
        print("[WebUI] Web端已启动：http://127.0.0.1:" + str(server.server_port) + "，回到终端按 Enter 退出。")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        server.shutdown()
        server.server_close()
    return server


if __name__ == "__main__":
    run_server(open_browser=True, blocking=True)
