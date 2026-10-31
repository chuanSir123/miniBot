"""验证"停止"能否在阻塞读期间立即生效（不依赖下一个 SSE 分片）。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.brain import VLMBrain


class SlowHandler(BaseHTTPRequestHandler):
    normal = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # 先吐一小段，然后长时间不再发任何数据（模拟长思考 / 长 TTFT）
        for text in ("你", "好"):
            chunk = {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.05)
        if self.normal:
            tail = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                              "prompt_tokens_details": {"cached_tokens": 8}}}
            self.wfile.write(f"data: {json.dumps(tail)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.2)
        try:
            self.wfile.write(b"data: [DONE]\n\n")
        except OSError:
            pass


def run(cancel_after, normal=False):
    handler = type("H", (SlowHandler,), {"normal": normal})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    brain = VLMBrain(base_url=f"http://127.0.0.1:{port}/v1", api_key="k", model="m")
    cancel = threading.Event()
    if cancel_after is not None:
        threading.Timer(cancel_after, cancel.set).start()

    started = time.monotonic()
    gen = brain.stream_chat_and_tools(
        [{"role": "user", "content": "hi"}], cancel_event=cancel
    )
    result = None
    while True:
        try:
            next(gen)
        except StopIteration as done:
            result = done.value
            break
    elapsed = time.monotonic() - started
    server.shutdown()
    return elapsed, result


if __name__ == "__main__":
    cases = (
        ("normal", None, True),
        ("cancel@1.0s", 1.0, False),
        ("cancel@0.1s", 0.1, False),
    )
    for label, after, normal in cases:
        elapsed, result = run(after, normal=normal)
        full, calls, usage = result
        print(
            json.dumps(
                {
                    "case": label,
                    "stop_latency_ms": None if after is None else round((elapsed - after) * 1000),
                    "total_ms": round(elapsed * 1000),
                    "partial": full,
                    "tool_calls": len(calls),
                    "cached_tokens": (usage or {}).get("prompt_tokens_details", {}).get("cached_tokens"),
                    "errored": "【" in full,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
