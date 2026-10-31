"""流收尾兼容性回归：上游不补 finish_reason / [DONE] 时不能把已答完的话判成失败。

覆盖四种真实网关形态：
  clean   有 finish_reason + [DONE] + 关闭
  nodone  有 finish_reason、无 [DONE]、连接不关（走 finish_grace 收尾）
  eof     无终结信号、直接断开（走 peer_eof 软收尾）
  dead    无终结信号、连接哑住（走 idle_stall 软收尾，不再干等满 request_timeout）
  cut     内容卡在半句话上就断开（必须报错，让 harness 重试而不是交付残句）
"""
import json
import socket
import threading
import time
import unittest

import core.brain as brain_mod
from core.brain import VLMBrain

SAY = ["看看", " (", "๑>◡<๑", ")", "🐾\n\n"]
CUT = ["猫咪喜欢晒太阳", "，因为它", "需要合成维生素D，所以", "经常躺在窗台上晒到全身暖"]


def _frame(obj) -> bytes:
    body = b"data: " + (obj if isinstance(obj, bytes)
                        else json.dumps(obj, ensure_ascii=False).encode()) + b"\n\n"
    return b"%X\r\n%s\r\n" % (len(body), body)


class FakeGateway:
    def __init__(self, mode: str):
        self.mode = mode
        self.held = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            part = conn.recv(65536)
            if not part:
                return
            buf += part
        head, rest = buf.split(b"\r\n\r\n", 1)
        need = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                need = int(line.split(b":")[1])
        while len(rest) < need:
            rest += conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        if self.mode == "slowfirst":
            # vLLM 典型开场：先给一个只带 role 的空内容块，然后才慢慢出字。
            conn.sendall(_frame({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}))
            time.sleep(2.5)
            for piece in SAY:
                conn.sendall(_frame({"choices": [{"index": 0, "delta": {"content": piece}}]}))
                time.sleep(0.02)
        elif self.mode == "midgap":
            for piece in SAY[:2]:
                conn.sendall(_frame({"choices": [{"index": 0, "delta": {"content": piece}}]}))
            time.sleep(2.5)
            for piece in SAY[2:]:
                conn.sendall(_frame({"choices": [{"index": 0, "delta": {"content": piece}}]}))
        else:
            for piece in (CUT if self.mode == "cut" else SAY):
                conn.sendall(_frame({"choices": [{"index": 0, "delta": {"content": piece}}]}))
                time.sleep(0.02)
        if self.mode in ("clean", "nodone", "slowfirst", "midgap"):
            conn.sendall(_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
        if self.mode == "clean":
            conn.sendall(_frame(b"[DONE]"))
            conn.sendall(b"0\r\n\r\n")
            conn.close()
            return
        if self.mode == "cut":
            conn.close()
            return
        self.held.append(conn)  # 其它模式：保持连接，一个字节都不再发

    def close(self):
        for c in self.held:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


class StreamTailTests(unittest.TestCase):
    TIMEOUT = 4.0
    STALL = 1.5

    def _run(self, mode):
        gw = FakeGateway(mode)
        real_load = brain_mod.load_config

        def fake_load():
            cfg = json.loads(json.dumps(real_load()))
            cfg["runtime"]["request_timeout_sec"] = self.TIMEOUT
            cfg["runtime"]["stream_finish_grace_sec"] = 1.0
            cfg["runtime"]["stream_idle_stall_sec"] = self.STALL
            return cfg

        brain_mod.load_config = fake_load
        try:
            brain = VLMBrain(f"http://127.0.0.1:{gw.port}/v1", "sk-test", "fake")
            started = time.monotonic()
            text, errors = [], []
            for kind, data in brain.stream_chat_and_tools([{"role": "user", "content": "hi"}]):
                if kind == "text_delta":
                    text.append(data)
                elif kind == "error":
                    errors.append(data)
            return "".join(text), errors, time.monotonic() - started
        finally:
            brain_mod.load_config = real_load
            gw.close()

    def test_clean_stream_closes_immediately(self):
        text, errors, elapsed = self._run("clean")
        self.assertEqual(errors, [])
        self.assertIn("🐾", text)
        self.assertLess(elapsed, 2.0)

    def test_finish_reason_without_done_is_accepted(self):
        text, errors, elapsed = self._run("nodone")
        self.assertEqual(errors, [])
        self.assertIn("🐾", text)
        self.assertLess(elapsed, self.STALL + 2.0, "finish_grace 没有生效")

    def test_peer_eof_without_terminal_marker_is_accepted(self):
        text, errors, _ = self._run("eof")
        self.assertEqual(errors, [], "内容已收全却因为缺 [DONE] 被误判为失败")
        self.assertIn("🐾", text)

    def test_dead_connection_soft_finishes_at_stall_threshold(self):
        text, errors, elapsed = self._run("dead")
        self.assertEqual(errors, [])
        self.assertIn("🐾", text)
        self.assertLess(elapsed, self.TIMEOUT, "死流仍在等满 request_timeout_sec")

    def test_truncated_reply_still_raises(self):
        _, errors, _ = self._run("cut")
        self.assertTrue(errors, "半句话的残流必须报错，交给 harness 重试")

    def test_empty_role_chunk_does_not_start_stall_clock(self):
        """vLLM 开场会先发 {"delta":{"role":"assistant","content":""}}，
        空内容块不能算"已出字"，否则 TTFT 会被 stall 阈值（这里是 1.5s）误杀。"""
        text, errors, _ = self._run("slowfirst")
        self.assertEqual(errors, [], "TTFT 阶段被当成了流卡死")
        self.assertIn("🐾", text)

    def test_mid_stream_gap_still_trips_stall(self):
        """已经吐过字之后再沉默超过 stall 阈值，仍应判定死流。"""
        _, errors, _ = self._run("midgap")
        self.assertTrue(errors, "出字之后长时间无数据没有被判定为死流")


if __name__ == "__main__":
    unittest.main()
