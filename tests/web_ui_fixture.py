"""Local browser-QA fixture; no model, user configuration or workspace writes.

Run: .venv/Scripts/python.exe tests/web_ui_fixture.py
This serves the real UI with synthetic API/SSE responses on 127.0.0.1:18081.
"""
import json
import queue
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HTML = Path(__file__).resolve().parents[1] / "webui" / "index.html"
SESSION = "web:main:visual_qa"
TITLE = "优化 Web 工作台与交互"
PROMPT = "优化 Web 页面，保留现有功能，先修复实际问题。"
PARAGRAPHS = [
    "我会先梳理现有页面与接口，再调整布局和任务进行中的展示，保留工作空间、图片、模型切换和配置能力。",
    "已定位到窄屏布局和历史回退的问题。接下来修复提交失败丢失输入，并检查任务执行过程中的状态。",
    "页面已调整为灰白工作台，工具过程改为轻量记录。正在检查手机宽度、文件预览和停止操作。",
]
CHANGE = {"rel_path": "webui/index.html", "path": "webui/index.html", "added": 166,
          "removed": 0, "diff_text": "--- a/webui/index.html\n+++ b/webui/index.html\n@@ -1,3 +1,5 @@\n <main>\n+  <section class=\"conversation\">\n+    <p>任务执行过程</p>\n+  </section>\n </main>"}
EVENTS = []
STATE = {"running": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            raw = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/workspaces":
            self.reply({"success": True, "data": [{"name": "main", "label": "miniBot"}]})
        elif path == "/api/models":
            self.reply({"success": True, "models": [{"id": "qa-model", "name": "本地验证模型", "model": "qa-model", "is_default": True}, {"id": "qa-secondary", "name": "对照验证模型", "model": "qa-secondary", "enable_thinking": True, "reasoning_effort": "medium"}]})
        elif path == "/api/sessions":
            self.reply({"success": True, "data": [{"session_id": SESSION, "title": TITLE, "is_active": STATE["running"]}]})
        elif path == "/api/workspace/files":
            self.reply({"success": True, "data": [{"name": "index.html", "path": "webui/index.html", "size": 4096}]})
        elif path == "/api/workspace/file":
            self.reply({"success": True, "data": {"content": "<main>\n  <p>任务执行过程</p>\n</main>"}})
        elif path == "/api/session/changes":
            self.reply({"success": True, "data": [CHANGE]})
        elif path == "/api/session/messages":
            messages = [{"role": "user", "content": PROMPT, "_id": 1}]
            if not STATE["running"]:
                messages.append({"role": "assistant", "content": "任务已停止，已完成的文件变更保留。", "file_changes": [CHANGE]})
            self.reply({"success": True, "data": messages, "is_active": STATE["running"]})
        elif path == "/api/stream":
            self.stream()
        else:
            self.reply({"success": False, "error": "fixture endpoint unavailable"}, 404)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events = queue.Queue()
        EVENTS.append(events)

        def send(name, data):
            self.wfile.write(("event: " + name + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            if STATE["running"]:
                send("active_snapshot", {"session_id": SESSION, "started_at": time.time() - 632,
                     "status": "正在检查页面布局与交互", "text": "", "tools": []})
                full = ""
                for index, paragraph in enumerate(PARAGRAPHS):
                    # Real Harness.full resets on each model call, rather than accumulating across tools.
                    full = paragraph + "\n\n"
                    send("text_delta", {"full": paragraph[:8], "delta": paragraph[:8]})
                    send("text_delta", {"full": full, "delta": paragraph[8:] + "\n\n"})
                    tool = "bash" if index < 2 else "write"
                    args = {"command": "检查页面布局"} if index < 2 else {"file_path": "webui/index.html", "content": "<main>\n  <p>任务执行过程</p>\n</main>"}
                    send("tool_call", {"tool": tool, "args": args})
                    send("tool_result", {"tool": tool, "result": {"ok": True, "added": 166 if index == 2 else 0, "removed": 0, "output": "验证通过"}})
                send("status", {"status": "正在检查手机宽度和任务控制…"})
            while True:
                try:
                    name, data = events.get(timeout=2)
                    send(name, data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            EVENTS.remove(events)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        path = urlparse(self.path).path
        if path == "/api/steer":
            for events in list(EVENTS):
                events.put(("steering", {"text": body.get("text", ""), "success": True}))
                events.put(("steering_applied", {"text": body.get("text", "")}))
            self.reply({"success": True})
        elif path == "/api/cancel":
            STATE["running"] = False
            for events in list(EVENTS):
                events.put(("cancelled", {"count": 0}))
            self.reply({"success": True})
        elif path == "/api/chat":
            # 仅用于本地 QA：模拟任务被接受并进入运行态
            STATE["running"] = True
            self.reply({"success": True, "session_id": SESSION,
                        "title": (body.get("prompt") or TITLE)[:24], "status": "running"})
        elif path == "/api/qa/complete":
            # 仅用于本地 QA：让前端收到一次 complete，验证排队消息自动发出
            STATE["running"] = False
            for events in list(EVENTS):
                events.put(("complete", {"reply": "本轮结束"}))
            self.reply({"success": True})
        else:
            self.reply({"success": False, "error": "fixture only supports steering/cancel"}, 400)


if __name__ == "__main__":
    print("UI fixture: http://127.0.0.1:18081 (synthetic data, no model calls)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 18081), Handler).serve_forever()
