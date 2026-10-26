import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
import threading

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.config import load_config, save_config

WEBUI_DIR = Path(__file__).resolve().parent
HTML_FILE = WEBUI_DIR / "index.html"


class ConfigServerHandler(BaseHTTPRequestHandler):

    def _set_headers(self, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(204)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/config"):
            if HTML_FILE.exists():
                self._set_headers(200, "text/html; charset=utf-8")
                with open(HTML_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._set_headers(404, "text/plain")
                self.wfile.write(b"404 Not Found")
        elif self.path == "/api/config":
            cfg = load_config()
            self._set_headers(200)
            self.wfile.write(json.dumps(cfg, ensure_ascii=False).encode("utf-8"))
        else:
            self._set_headers(404)
            self.wfile.write(b'{"error": "Not Found"}')

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req_data = {}

        if self.path == "/api/config":
            save_config(req_data)
            self._set_headers(200)
            self.wfile.write(b'{"success": true}')
            return

        elif self.path == "/api/test/jev":
            base_url = req_data.get("base_url", "").rstrip("/")
            api_key = req_data.get("api_key", "").strip()
            model = req_data.get("model", "startlux-decision-27b-autothink")

            if not api_key:
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": "API Key 不能为空"}).encode("utf-8"))
                return

            endpoint = f"{base_url}/decisions" if not base_url.endswith("/decisions") else base_url
            payload = json.dumps({
                "model": model,
                "state": "System health probe.",
                "questions": {
                    "probe": {
                        "type": "noul",
                        "instructions": "Is the system responsive?"
                    }
                }
            }).encode("utf-8")

            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "MiniBot-Client/1.0"
                },
                method="POST"
            )

            start_t = time.time()
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_body = resp.read()
                    latency_ms = int((time.time() - start_t) * 1000)
                    self._set_headers(200)
                    self.wfile.write(json.dumps({
                        "success": True,
                        "latency_ms": latency_ms,
                        "data": resp_body.decode("utf-8", errors="ignore")[:200]
                    }).encode("utf-8"))
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": f"HTTP {e.code}: {err_text[:120]}"}).encode("utf-8"))
            except Exception as e:
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif self.path == "/api/test/vlm":
            base_url = req_data.get("base_url", "").rstrip("/")
            api_key = req_data.get("api_key", "").strip()
            model = req_data.get("model", "gpt-4o")

            if not api_key:
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": "API Key 不能为空"}).encode("utf-8"))
                return

            endpoint = f"{base_url}/chat/completions" if not base_url.endswith("/chat/completions") else base_url
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5
            }).encode("utf-8")

            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "MiniBot-Client/1.0"
                },
                method="POST"
            )

            start_t = time.time()
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    latency_ms = int((time.time() - start_t) * 1000)
                    self._set_headers(200)
                    self.wfile.write(json.dumps({
                        "success": True,
                        "latency_ms": latency_ms
                    }).encode("utf-8"))
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": f"HTTP {e.code}: {err_text[:120]}"}).encode("utf-8"))
            except Exception as e:
                self._set_headers(200)
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        self._set_headers(404)
        self.wfile.write(b'{"error": "Unknown API endpoint"}')

    def log_message(self, format, *args):
        # Mute normal HTTP logging to keep console clean
        return


def run_server(port: int = 18080, open_browser: bool = False, blocking: bool = True):
    server = HTTPServer(("127.0.0.1", port), ConfigServerHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"[WebUI] 配置中心已启动: {url}")

    if open_browser:
        webbrowser.open(url)

    if blocking:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("[WebUI] 服务已停止")
    else:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server


if __name__ == "__main__":
    run_server(open_browser=True, blocking=True)
