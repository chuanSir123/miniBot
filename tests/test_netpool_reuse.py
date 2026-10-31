"""连接池复用行为的回归测试。

验证三件事：
  1. 连续请求复用同一条 TCP 连接（不重新握手）；
  2. 空闲时间超过旧版 45 秒窗口后仍然复用（窗口已由 socket 活性探测取代）；
  3. 对端悄悄关掉的僵尸连接会被探测出来并自动重建，不会把错误抛给上层。
"""
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from core.netpool import open_request


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    conns = []      # 每条连接上服务过的请求所对应的 socket 对象（持引用防 id 复用）
    mode = "keep"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).conns.append(self.connection)
        body = b'{"ok":true}'
        if type(self).mode == "close":
            # 模拟网关空闲回收：响应发完就关。关闭标记必须在写 body 之前落定，
            # 否则客户端收完 body 后抢在标记之前发出下一请求，就不算"僵尸连接"了。
            self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *args):
        pass


class NetPoolReuseTests(unittest.TestCase):
    def setUp(self):
        _Handler.conns = []
        _Handler.mode = "keep"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def _connections(self):
        """服务过请求的连接条数（同一 keep-alive 连接发多次请求仍算 1 条）。"""
        return len({id(conn) for conn in _Handler.conns})

    def _post(self):
        request = urllib.request.Request(
            self.url, data=b"{}", headers={"Content-Type": "application/json"},
            method="POST",
        )
        with open_request(request, timeout=5) as response:
            return response.read()

    def test_sequential_requests_reuse_one_connection(self):
        for _ in range(3):
            self.assertEqual(self._post(), b'{"ok":true}')
        self.assertEqual(self._connections(), 1, "3 次请求应共用 1 条连接")

    def test_idle_longer_than_old_45s_window_still_reuses(self):
        self._post()
        # 把时钟往前推 46 秒：旧实现会因为 "_POOL_TTL_SEC = 45" 重建连接。
        with patch("core.netpool.time.monotonic", return_value=time.monotonic() + 46):
            self._post()
        self.assertEqual(self._connections(), 1, "空闲超过 45 秒仍应复用")

    def test_dead_connection_is_detected_and_rebuilt(self):
        _Handler.mode = "close"
        self.assertEqual(self._post(), b'{"ok":true}')
        time.sleep(0.2)  # 等服务端把 FIN 送到
        _Handler.mode = "keep"
        # 池里的那条连接已被对端关闭，探测后丢弃重建，请求仍然成功。
        self.assertEqual(self._post(), b'{"ok":true}')
        self.assertEqual(self._connections(), 2, "僵尸连接应被探测并重建")

    def test_concurrent_requests_do_not_thrash(self):
        results, errors = [], []

        def worker():
            try:
                results.append(self._post())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)


if __name__ == "__main__":
    unittest.main()
