import asyncio
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from test_core import Fixture

from core import config
from core.brain import VLMBrain
from core.jev import JevClient
from channels.napcat_channel import NapCatChannel, connect as ws_connect
from channels.napcat_manager import NapCatManager
from webui.server import run_server


class ConfigHTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        config.save_config(
            {
                "vlm": {"api_key": "TEST_SECRET"},
                "channels": {"napcat": {"access_token": "TEST_TOKEN"}},
            }
        )
        self.server = run_server(port=0, blocking=False)
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, method="GET", body=None, headers=None):
        self.connection.request(
            method, path, json.dumps(body) if body is not None else None, headers or {}
        )
        response = self.connection.getresponse()
        result = response.read()
        return response, result

    def auth(self):
        response, _ = self.request("/")
        return response.getheader("Set-Cookie").split(";", 1)[0]

    def test_unauthenticated_and_cross_origin_blocked(self):
        response, body = self.request("/api/config")
        self.assertEqual(response.status, 403)
        cookie = self.auth()
        response, body = self.request(
            "/api/config", headers={"Cookie": cookie, "Origin": "https://evil.example"}
        )
        self.assertEqual(response.status, 403)
        response, body = self.request(
            "/api/config", headers={"Cookie": cookie, "Host": "evil.example"}
        )
        self.assertEqual(response.status, 403)

    def test_secrets_redacted_preserved_and_explicitly_cleared(self):
        cookie = self.auth()
        headers = {
            "Cookie": cookie,
            "X-MiniBot-Request": "1",
            "Content-Type": "application/json",
        }
        response, body = self.request("/api/config", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"TEST_SECRET", body)
        self.assertNotIn(b"TEST_TOKEN", body)
        response, _ = self.request(
            "/api/config", "POST", {"vlm": {"api_key": "", "model": "updated"}}, headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(config.load_config()["vlm"]["api_key"], "TEST_SECRET")
        response, _ = self.request(
            "/api/config", "POST", {"vlm": {"clear_api_key": True}}, headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(config.load_config()["vlm"]["api_key"], "")

    def test_write_requires_custom_header_and_valid_schema(self):
        cookie = self.auth()
        response, _ = self.request("/api/config", "POST", {}, {"Cookie": cookie})
        self.assertEqual(response.status, 403)
        response, _ = self.request(
            "/api/config",
            "POST",
            {"runtime": {"max_workers": 0}},
            {"Cookie": cookie, "X-MiniBot-Request": "1"},
        )
        self.assertEqual(response.status, 400)

    def test_reuses_same_port(self):
        self.assertIs(run_server(self.server.server_port, blocking=False), self.server)

    def test_anysearch_registration_keeps_one_time_secret_private(self):
        cookie = self.auth()
        headers = {"Cookie": cookie, "X-MiniBot-Request": "1"}
        secret = "as_sk_TEST_ONLY_REGISTRATION"
        with patch(
            "core.default_skills.anysearch_request",
            return_value={
                "code": 0,
                "data": {"username": "test@example.com", "api_key": {"key": secret}},
            },
        ) as register:
            response, body = self.request(
                "/api/anysearch/register",
                "POST",
                {"email": "test@example.com"},
                headers,
            )
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.assertNotIn(secret.encode(), body)
        self.assertEqual(config.load_config()["anysearch"]["api_key"], secret)
        self.assertEqual(register.call_args.args[0], "/v1/auth/email/register")
        response, body = self.request("/api/config", headers={"Cookie": cookie})
        self.assertNotIn(secret.encode(), body)
        self.assertTrue(json.loads(body)["anysearch"]["api_key_configured"])
        response, _ = self.request(
            "/api/config", "POST", {"anysearch": {"api_key": ""}}, headers
        )
        self.assertEqual(config.load_config()["anysearch"]["api_key"], secret)
        # 单文件配置：Key 落 config.json，local 文件不再生成。
        self.assertIn(secret, config.CONFIG_FILE_PATH.read_text(encoding="utf-8"))
        self.assertFalse(
            config.CONFIG_FILE_PATH.with_name("config.local.json").exists()
        )

    def test_anysearch_existing_email_error_does_not_retry_or_save(self):
        cookie = self.auth()
        with patch(
            "core.default_skills.anysearch_request",
            side_effect=ValueError("email_already_registered"),
        ) as register:
            response, body = self.request(
                "/api/anysearch/register",
                "POST",
                {"email": "test@example.com"},
                {"Cookie": cookie, "X-MiniBot-Request": "1"},
            )
        self.assertFalse(json.loads(body)["success"])
        self.assertEqual(register.call_count, 1)
        self.assertEqual(config.load_config()["anysearch"]["api_key"], "")


class ModelWireTests(Fixture):
    def setUp(self):
        super().setUp()
        self.payloads = []
        self.mode = "valid"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                outer.payloads.append((self.path, payload))
                if self.path.endswith(("/decisions", "/systemone")):
                    body = json.dumps(
                        {
                            "answers": {
                                "route": {
                                    "type": "choice",
                                    "choice": "chat",
                                    "confidence": 0.99,
                                    "none_probability": 0.8
                                    if outer.mode == "none"
                                    else 0.01,
                                },
                                "intervene": {"type": "noul", "noul": 0.9},
                            }
                        }
                    ).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/event-stream"
                    if payload.get("stream")
                    else "application/json",
                )
                self.end_headers()
                if not payload.get("stream"):
                    self.wfile.write(b'{"choices":[{"message":{"content":"summary"}}]}')
                    return
                if outer.mode == "gateway_error":
                    chunks = [
                        {"choices": [{"delta": {}, "finish_reason": None}]},
                        {
                            "error": json.dumps(
                                {
                                    "error": {
                                        "message": "System message must be at the beginning.",
                                        "code": 400,
                                    }
                                }
                            )
                        },
                    ]
                elif outer.mode in ("partial_tool_done", "wrong_tool_stop"):
                    chunks = [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "test_call",
                                                "function": {
                                                    "name": "echo",
                                                    "arguments": '{"text":"ping"}'
                                                    if outer.mode == "wrong_tool_stop"
                                                    else '{"text":',
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "stop"
                                    if outer.mode == "wrong_tool_stop"
                                    else None,
                                }
                            ]
                        }
                    ]
                elif outer.mode == "text_done":
                    chunks = [
                        {
                            "choices": [
                                {"delta": {"content": "hello"}, "finish_reason": None}
                            ]
                        }
                    ]
                elif outer.mode == "partial":
                    chunks = [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "c",
                                                "function": {
                                                    "name": "write",
                                                    "arguments": "{",
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                elif outer.mode == "length":
                    chunks = [
                        {
                            "choices": [
                                {
                                    "delta": {"content": "partial"},
                                    "finish_reason": "length",
                                }
                            ]
                        }
                    ]
                else:
                    chunks = [
                        {"choices": [{"delta": {"content": "hello"}}]},
                        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 2,
                                "total_tokens": 12,
                            },
                        },
                    ]
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                if outer.mode not in ("partial", "no_done", "gateway_error"):
                    self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base = f"http://127.0.0.1:{self.server.server_port}/v1"
        config.save_config(
            {
                "vlm": {"base_url": base, "api_key": "FAKE"},
                "jev": {"base_url": base, "api_key": "FAKE"},
            }
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        super().tearDown()

    @staticmethod
    def consume(generator):
        events = []
        while True:
            try:
                events.append(next(generator))
            except StopIteration as done:
                return events, done.value

    def test_real_http_sse_usage_and_hot_reload(self):
        brain = VLMBrain()
        events, result = self.consume(
            brain.stream_chat_and_tools([{"role": "user", "content": "hi"}])
        )
        self.assertEqual(result[0], "hello")
        self.assertEqual(result[2]["total_tokens"], 12)
        self.assertFalse(any(e[0] == "error" for e in events))
        config.save_config({"vlm": {"model": "hot-model"}})
        brain.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(self.payloads[-1][1]["model"], "hot-model")
        self.assertEqual(
            JevClient().decide("state", {"route": {"type": "choice", "options": ["chat", "action"]}}),
            {"route": "chat"})
        request = self.payloads[-1][1]["questions"]["route"]
        self.assertIn("criteria", request)
        self.assertNotIn("options", request)
        self.assertEqual(
            JevClient().decide(
                "state", {"intervene": {"type": "noul", "instructions": "yes?"}}
            )["intervene"],
            0.9,
        )

    def test_systemone_full_endpoint_and_raw_metadata(self):
        base = f"http://127.0.0.1:{self.server.server_port}/v1/systemone"
        client = JevClient(base, "FAKE", "vllm-sr/d3-turbo")
        result = client.decide_raw(
            "state",
            {
                "route": {
                    "type": "choice",
                    "instructions": "Select chat",
                    "criteria": {"chat": "Chat", "action": "Act"},
                }
            },
        )
        self.assertEqual(self.payloads[-1][0], "/v1/systemone")
        self.assertEqual(set(self.payloads[-1][1]), {"model", "state", "questions"})
        self.assertEqual(result["answers"]["route"]["confidence"], 0.99)
        self.assertEqual(
            client.endpoint("https://example.org"), "https://example.org/v1/systemone"
        )
        self.assertEqual(
            client.endpoint("https://example.org/v1"),
            "https://example.org/v1/systemone",
        )

    def test_systemone_bearer_prefix_and_rate_limit_cooldown(self):
        import io
        import urllib.error
        import urllib.request
        from email.message import Message

        client = JevClient(
            f"http://127.0.0.1:{self.server.server_port}/v1/systemone",
            "Bearer FAKE",
            "test",
        )
        q = {"probe": {"type": "noul", "instructions": "ready?"}}
        request = []

        class Response:
            def __enter__(self):
                return io.BytesIO(b'{"answers":{"probe":{"type":"noul","noul":0.9}}}')

            def __exit__(self, *args):
                return False

        def success(req, **kwargs):
            request.append(req)
            return Response()

        with patch("core.jev.open_request", side_effect=success):
            client.decide_raw("test", q)
        self.assertEqual(request[0].get_header("Authorization"), "Bearer FAKE")
        headers = Message()
        headers["Retry-After"] = "60"
        error = urllib.error.HTTPError(
            client.endpoint(client.base_url),
            429,
            "limit",
            headers,
            io.BytesIO(b'{"error":{"code":"rate_limit_exceeded"}}'),
        )
        with patch("core.jev.open_request", side_effect=error) as transport:
            with self.assertRaisesRegex(ValueError, "HTTP 429"):
                client.decide_raw("test", q)
            with self.assertRaisesRegex(ValueError, "冷却"):
                client.decide_raw("test", q)
            self.assertEqual(transport.call_count, 1)

    def test_systemone_score_and_invalid_response(self):
        from unittest.mock import patch

        client = JevClient()
        q = {
            "urgency": {
                "type": "score",
                "instructions": "How urgent?",
                "criteria": ["Low", "Normal", "Urgent"],
            }
        }
        with patch.object(
            client,
            "decide_raw",
            return_value={"answers": {"urgency": {"type": "score", "score": 1.998}}},
        ):
            self.assertEqual(client.decide("outage", q), {"urgency": 1.998})
        for answer in (
            {"type": "score", "score": float("nan")},
            {"type": "score", "score": 3},
        ):
            with patch.object(
                client, "decide_raw", return_value={"answers": {"urgency": answer}}
            ):
                self.assertEqual(client.decide("outage", q), {})
        with self.assertRaises(ValueError):
            client.decide_raw(
                "state", {"route": {"type": "choice", "criteria": {"only": "one"}}}
            )

    def test_timeout_diagnostics_identify_headers_and_stream_without_replay(self):
        import urllib.error
        from unittest.mock import MagicMock

        for phase in ("headers", "first_line", "partial"):
            brain = VLMBrain()
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            partial = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"write","arguments":"{"}}]}}]}\n'
            response.readline.side_effect = ([partial, TimeoutError("The read operation timed out")]
                                             if phase == "partial" else TimeoutError("The read operation timed out"))
            with patch("core.brain._open_request") as opening, patch("core.brain.log") as logs:
                if phase == "headers":
                    opening.side_effect = urllib.error.URLError(TimeoutError("timed out"))
                else:
                    opening.return_value = response
                events, result = self.consume(brain.stream_chat_and_tools([{"role": "user", "content": "hi"}]))
            self.assertEqual(opening.call_count, 1)
            self.assertEqual(result[1], [])
            self.assertIn("对话模型请求超时", result[0])
            self.assertTrue(any(kind == "error" for kind, _ in events))
            error = next(c.kwargs for c in logs.call_args_list if c.args[1] == "request.error")
            expected = {"headers": "等待连接或响应头", "first_line": "等待首行流数据", "partial": "读取流数据"}[phase]
            self.assertEqual(error["stage"], expected)
            self.assertTrue(error["timeout"])
            self.assertEqual(error["received_bytes"], len(partial) if phase == "partial" else 0)
            self.assertIn(error["request_id"], result[0])

    def test_http_520_is_distinguished_from_local_timeout(self):
        import io
        import urllib.error
        from email.message import Message
        headers = Message()
        headers["cf-ray"] = "test-ray"
        error = urllib.error.HTTPError("http://localhost", 520, "unknown origin", headers, io.BytesIO(b'{"error":"invalid origin response"}'))
        brain = VLMBrain()
        with patch("core.brain._open_request", side_effect=error), patch("core.brain.log") as logs:
            events, result = self.consume(brain.stream_chat_and_tools([{"role": "user", "content": "hi"}]))
        self.assertEqual(result[1], [])
        self.assertIn("HTTP 520", result[0])
        self.assertIn("并非本机等待超时", result[0])
        diagnostic = next(c.kwargs for c in logs.call_args_list if c.args[1] == "request.error")
        self.assertEqual(diagnostic["cf_ray"], "test-ray")
        self.assertTrue(any(c.args[1] == "response.error_detail" for c in logs.call_args_list))

    def test_partial_or_length_stream_has_no_executable_tools(self):
        for mode in ("partial", "length"):
            self.mode = mode
            events, result = self.consume(
                VLMBrain().stream_chat_and_tools([{"role": "user", "content": "hi"}])
            )
            self.assertEqual(result[1], [])
            self.assertTrue(any(e[0] == "error" for e in events))

    def test_jev_none_probability_falls_back(self):
        self.mode = "none"
        # none_probability 偏高说明"哪个都不合适"，此时不该采信任何选项。
        self.assertEqual(
            JevClient().decide("state", {"route": {"type": "choice", "options": ["chat", "action"]}}),
            {})

    def test_multiple_system_messages_are_merged_before_sending(self):
        messages = [
            {"role": "system", "content": "static policy"},
            {"role": "system", "content": "scoped context"},
            {"role": "user", "content": "hi"},
        ]
        events, result = self.consume(VLMBrain().stream_chat_and_tools(messages))
        sent = self.payloads[-1][1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user"])
        self.assertIn("static policy", sent[0]["content"])
        self.assertIn("scoped context", sent[0]["content"])
        self.assertEqual(len(messages), 3)
        self.assertFalse(any(event[0] == "error" for event in events))
        self.assertEqual(result[0], "hello")

    def test_gateway_error_is_visible_and_not_misreported_as_incomplete(self):
        self.mode = "gateway_error"
        events, result = self.consume(
            VLMBrain().stream_chat_and_tools([{"role": "user", "content": "hi"}])
        )
        self.assertIn("System message must be at the beginning", result[0])
        self.assertNotIn("响应不完整", result[0])
        self.assertEqual(result[1], [])
        self.assertTrue(any(event[0] == "error" for event in events))

    def test_valid_terminal_or_text_done_does_not_require_both_markers(self):
        for mode in ("no_done", "text_done"):
            self.mode = mode
            events, result = self.consume(
                VLMBrain().stream_chat_and_tools([{"role": "user", "content": "hi"}])
            )
            self.assertEqual(result[0], "hello")
            self.assertFalse(any(event[0] == "error" for event in events))

    def test_partial_tool_or_wrong_finish_cannot_authorize_execution(self):
        for mode in ("partial_tool_done", "wrong_tool_stop"):
            self.mode = mode
            events, result = self.consume(
                VLMBrain().stream_chat_and_tools([{"role": "user", "content": "hi"}])
            )
            self.assertEqual(result[1], [])
            self.assertTrue(any(event[0] == "error" for event in events))

    def test_connection_reset_before_response_is_retried_once(self):
        """连接在拿到任何响应字节前被重置：换一条新连接重试一次。"""
        import core.netpool as netpool_module

        original = netpool_module._pool_acquire
        attempts = [0]

        class ResettingConnection:
            sock = None

            def request(self, *_args, **_kwargs):
                raise ConnectionResetError(10054, "connection reset")

            def close(self):
                pass

        def acquire(key, scheme, host, port, timeout):
            attempts[0] += 1
            if attempts[0] == 1:
                return ResettingConnection(), False
            return original(key, scheme, host, port, timeout)

        with patch.object(netpool_module, "_pool_acquire", side_effect=acquire):
            events, result = self.consume(
                VLMBrain().stream_chat_and_tools([{"role": "user", "content": "hi"}])
            )
        self.assertEqual(attempts[0], 2)
        self.assertEqual(result[0], "hello")
        self.assertFalse(any(event[0] == "error" for event in events))

    def test_authentication_error_is_not_retried(self):
        """4xx 是确定性错误：不重试，直接以 HTTPError 暴露给上层。"""
        import urllib.error
        import urllib.request
        import core.netpool as netpool_module
        from core.netpool import open_request

        class RawResponse:
            status = 401
            reason = "unauthorized"
            headers = {}

            def read(self, *_args):
                return b'{"error":"unauthorized"}'

            def readline(self, *_args):
                return b""

            def isclosed(self):
                return True

            def close(self):
                pass

        class UnauthorizedConnection:
            sock = None

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return RawResponse()

            def close(self):
                pass

        request = urllib.request.Request(
            "http://127.0.0.1:1/v1/chat/completions",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(
            netpool_module, "_pool_acquire", return_value=(UnauthorizedConnection(), False)
        ) as acquire:
            with self.assertRaises(urllib.error.HTTPError):
                open_request(request, 1)
        self.assertEqual(acquire.call_count, 1)

    def test_stream_tail_without_done_or_close_does_not_hang(self):
        """网关发完 finish_reason 后既不补 [DONE] 也不关连接时必须主动收尾。"""
        from unittest.mock import MagicMock

        brain = VLMBrain()
        response = MagicMock()
        response.__enter__.return_value = response
        lines = [
            b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        ]

        def readline(_size=0):
            if lines:
                return lines.pop(0)
            # 内容已结束但上游保持连接：readline 只会阻塞到读超时。
            raise TimeoutError("The read operation timed out")

        response.readline.side_effect = readline
        started = time.monotonic()
        with patch("core.brain._open_request", return_value=response), patch(
            "core.brain.log"
        ) as logs:
            events, result = self.consume(
                brain.stream_chat_and_tools([{"role": "user", "content": "hi"}])
            )
        elapsed = time.monotonic() - started
        self.assertEqual(result[0], "hello")
        self.assertEqual(result[1], [])
        self.assertFalse(any(kind == "error" for kind, _ in events))
        self.assertLess(elapsed, 5, "收尾阶段不应一直等到 request_timeout_sec")
        self.assertTrue(
            any(c.args[1] == "stream.tail.closed" for c in logs.call_args_list)
        )
        # 收尾阶段应把读超时收紧，而不是继续沿用整轮超时。
        response.fp.raw._sock.settimeout.assert_called()

    def test_done_marker_without_space_is_recognized(self):
        from unittest.mock import MagicMock

        brain = VLMBrain()
        response = MagicMock()
        response.__enter__.return_value = response
        lines = [
            b'data:{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n',
            b"data:[DONE]\n\n",
        ]
        response.readline.side_effect = lambda _size=0: lines.pop(0) if lines else b""
        with patch("core.brain._open_request", return_value=response):
            events, result = self.consume(
                brain.stream_chat_and_tools([{"role": "user", "content": "hi"}])
            )
        self.assertEqual(result[0], "hi")
        self.assertFalse(any(kind == "error" for kind, _ in events))

    def test_thinking_fields_have_no_extra_body_wrapper(self):
        config.save_config(
            {
                "vlm": {
                    "enable_thinking": True,
                    "thinking_fields": {"thinking": {"type": "enabled"}},
                }
            }
        )
        payload = VLMBrain()._build_payload([])
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("extra_body", payload)


class NapCatConfigTests(Fixture):
    def test_documented_config_preserves_existing_servers_and_token(self):
        config.save_config(
            {
                "channels": {
                    "napcat": {
                        "access_token": "FAKE_TOKEN",
                        "ws_url": "ws://127.0.0.1:3456",
                    }
                }
            }
        )
        manager = NapCatManager(self.root / "napcat")
        folder = manager.napcat_dir / "config"
        folder.mkdir(parents=True)
        (folder / "onebot11.json").write_text(
            json.dumps(
                {"network": {"websocketServers": [{"name": "Existing", "port": 9999}]}}
            )
        )
        manager._ensure_config("123")
        obj = json.loads((folder / "onebot11.json").read_text(encoding="utf-8"))
        self.assertEqual(obj["network"]["websocketServers"][0]["name"], "Existing")
        ours = obj["network"]["websocketServers"][1]
        self.assertEqual(
            (ours["host"], ours["port"], ours["token"]),
            ("127.0.0.1", 3456, "FAKE_TOKEN"),
        )
        self.assertTrue((folder / "onebot11_123.json").exists())


class QQWireTests(Fixture):
    def test_authorization_group_mentions(self):
        config.save_config(
            {"channels": {"napcat": {"owner_ids": ["1"], "allowed_group_ids": ["100"]}}}
        )
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 1,
            "group_id": 100,
            "self_id": 99,
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
        self.assertFalse(NapCatChannel.allowed(event))
        event["message"].append({"type": "at", "data": {"qq": "99"}})
        self.assertTrue(NapCatChannel.allowed(event))
        event["user_id"] = 2
        self.assertFalse(NapCatChannel.allowed(event))

    @unittest.skipUnless(ws_connect is not None, "optional websockets not installed")
    def test_real_socket_slow_reply_does_not_block_acks_and_stop(self):
        from websockets.asyncio.server import serve

        ready = threading.Event()
        state = {}
        received = []

        async def handler(ws):
            event = {
                "post_type": "message",
                "message_type": "private",
                "user_id": 1,
                "self_id": 99,
                "message_id": 42,
                "raw_message": "hello",
            }
            await ws.send(json.dumps(event))
            await ws.send(json.dumps(event))
            async for raw in ws:
                action = json.loads(raw)
                received.append(action)
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 50},
                            "echo": action["echo"],
                        }
                    )
                )

        async def server_main():
            async with serve(handler, "127.0.0.1", 0) as server:
                state["port"] = server.sockets[0].getsockname()[1]
                ready.set()
                await state["stop"].wait()

        def server_thread():
            loop = asyncio.new_event_loop()
            state["loop"] = loop
            asyncio.set_event_loop(loop)
            state["stop"] = asyncio.Event()
            try:
                loop.run_until_complete(server_main())
            finally:
                loop.close()

        thread = threading.Thread(target=server_thread, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(3))
        config.save_config(
            {
                "channels": {
                    "napcat": {
                        "ws_url": f"ws://127.0.0.1:{state['port']}",
                        "auto_launch": False,
                        "owner_ids": ["1"],
                    }
                }
            }
        )
        future = concurrent.futures.Future()
        callbacks = []
        channel = NapCatChannel(
            on_message_callback=lambda text, event: callbacks.append(text) or future
        )
        try:
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "WS_PROXY": "http://127.0.0.1:1",
                    "ALL_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "http_proxy": "http://127.0.0.1:1",
                    "https_proxy": "http://127.0.0.1:1",
                    "ws_proxy": "http://127.0.0.1:1",
                    "all_proxy": "http://127.0.0.1:1",
                    "no_proxy": "",
                },
            ):
                channel.start()
                self.assertTrue(
                    channel.connected.wait(3),
                    "loopback connection must bypass the broken proxy",
                )
            deadline = time.monotonic() + 3
            while not callbacks and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(callbacks, ["hello"])
            self.assertTrue(channel.send_notification("qq_private", "1", "proactive"))
            future.set_result("answer")
            deadline = time.monotonic() + 3
            while len(received) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(received), 2)
            self.assertIsInstance(received[0]["params"]["message"], list)
        finally:
            channel.stop()
            state["loop"].call_soon_threadsafe(state["stop"].set)
            thread.join(3)
        self.assertFalse(channel._thread.is_alive())
        self.assertFalse(thread.is_alive())


class NapCatRestartHandoffTests(Fixture):
    """/restart 只重启本体：NapCat 进程所有权要能交接给下一个实例。"""

    def _manager(self):
        manager = NapCatManager(self.root / "napcat")
        manager.napcat_dir.mkdir(parents=True, exist_ok=True)
        return manager

    def test_adopt_takes_over_a_live_napcat(self):
        manager = self._manager()
        manager._remember_owner(os.getpid())
        adopted = self._manager()
        self.assertTrue(adopted.adopt())
        self.assertEqual(adopted._owned_pid, os.getpid())
        # 接管来的实例没有 Popen 句柄，不能被当成“自己拉起的进程”重复清理
        self.assertIsNone(adopted.process)

    def test_detach_keeps_ownership_for_the_next_instance(self):
        manager = self._manager()
        manager._remember_owner(os.getpid())
        manager.detach()
        self.assertIsNone(manager.process)
        self.assertTrue(manager._owner_state.exists())

    def test_stop_clears_ownership(self):
        manager = self._manager()
        manager._remember_owner(999999)
        manager.stop()
        self.assertFalse(manager._owner_state.exists())


class NapCatBootstrapTests(Fixture):
    def prepare(self):
        manager = NapCatManager(self.root / "napcat")
        manager.napcat_dir.mkdir()
        for name in (
            "NapCatWinBootMain.exe",
            "NapCatWinBootHook.dll",
            "napcat.mjs",
            "qqnt.json",
        ):
            (manager.napcat_dir / name).write_text("{}")
        executable = self.root / "QQ.exe"
        executable.write_text("fixture")
        config.save_config({"channels": {"napcat": {"qq_path": str(executable)}}})
        return manager, executable

    def test_native_command_never_forces_quick_login(self):
        """带 QQ 号位置参数会被启动器拼成 QQ 的 `-q`：实测会卡在快速登录里不出二维码。"""
        manager, executable = self.prepare()
        original = os.environ.copy()
        command, env = manager._build_command("123456")
        self.assertEqual(len(command), 3)
        self.assertEqual(command[1], str(executable.resolve()))
        self.assertNotIn("123456", command)
        self.assertNotIn("-q", command)
        self.assertNotIn("ACCOUNT", env)
        self.assertNotIn("NAPCAT_QUICK_ACCOUNT", env)
        self.assertEqual(os.environ, original)
        self.assertTrue(Path(env["NAPCAT_LOAD_PATH"]).is_file())
        self.assertEqual(
            env["NAPCAT_MAIN_PATH"], str(manager.napcat_dir.resolve() / "napcat.mjs")
        )

    def test_no_quick_login_does_not_invent_argument(self):
        manager, _ = self.prepare()
        self.assertEqual(len(manager._build_command()[0]), 3)
        with self.assertRaises(ValueError):
            manager._build_command("-q")

    def test_readiness_separates_bootstrap_from_server(self):
        manager, _ = self.prepare()
        with patch.object(manager, "_listening", return_value=False):
            state = manager.readiness()
        self.assertFalse(state["onebot_listening"])
        self.assertFalse(state["webui_listening"])

    def test_loopback_proxy_disabled_on_websockets15(self):
        def modern_connect(uri, *, proxy=True, **kwargs):
            pass

        with patch("channels.napcat_channel.connect", modern_connect):
            options = NapCatChannel.connection_options(
                {
                    "ws_url": "ws://127.0.0.1:3001",
                    "access_token": "fixture",
                    "use_proxy": True,
                }
            )
        self.assertIsNone(options["proxy"])
        self.assertEqual(
            options["additional_headers"]["Authorization"], "Bearer fixture"
        )

    def test_websockets14_does_not_receive_unsupported_proxy_option(self):
        def old_connect(uri, *, additional_headers=None, **kwargs):
            pass

        with patch("channels.napcat_channel.connect", old_connect):
            options = NapCatChannel.connection_options(
                {"ws_url": "ws://127.0.0.1:3001"}
            )
        self.assertNotIn("proxy", options)

    def test_repeated_connection_refusal_logs_are_throttled(self):
        import contextlib
        import io

        channel = NapCatChannel()
        stream = io.StringIO()
        error = ConnectionRefusedError(10061, "refused")
        with contextlib.redirect_stdout(stream):
            for _ in range(5):
                channel._report_connection_error(error, "ws://127.0.0.1:3001")
        self.assertEqual(stream.getvalue().count("[QQ]"), 1)
