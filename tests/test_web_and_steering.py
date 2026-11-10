import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.harness import AgentHarness
from core.jev import JevClient
from core.memory import MiniMemory
from core.workspace import (
    create_workspace,
    list_workspace_files,
    list_workspaces,
    read_workspace_file,
    workspace_for,
)
from main import MiniBotApp
from webui.server import broadcast_sse, register_sse_subscriber, unregister_sse_subscriber


class TestWorkspaceAndSteering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.memory = MiniMemory(str(Path(self.temp_dir) / "test.db"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_workspace_routing_and_isolation(self):
        # 1. Main workspace
        ws_main = workspace_for("main")
        self.assertTrue(str(ws_main).endswith("main"))

        # 2. Local / pet scope fallbacks to main workspace
        ws_local = workspace_for("local")
        self.assertEqual(ws_local, ws_main)
        ws_pet = workspace_for("pet")
        self.assertEqual(ws_pet, ws_main)

        # 3. QQ Private chat workspace
        ws_qq_priv = workspace_for("qq_private_123456")
        self.assertTrue(str(ws_qq_priv).endswith("qq_private_123456"))

        # 4. QQ Group chat workspace
        ws_qq_grp = workspace_for("qq_group_987654")
        self.assertTrue(str(ws_qq_grp).endswith("qq_group_987654"))

        # 5. Custom workspace
        ws_custom = create_workspace("my_project")
        self.assertEqual(ws_custom["name"], "my_project")

        # 6. List workspaces
        all_ws = list_workspaces()
        self.assertTrue(any(w["name"] == "main" and w["is_default"] for w in all_ws))

        # 7. File creation and reading in workspace
        test_file = ws_main / "hello.py"
        test_file.write_text("print('MiniBot Harness')", encoding="utf-8")
        files = list_workspace_files("main")
        self.assertTrue(any(f["name"] == "hello.py" for f in files))
        read_res = read_workspace_file("main", "hello.py")
        self.assertEqual(read_res["content"], "print('MiniBot Harness')")

    def test_jev_classify_steering_intent_heuristics(self):
        jev = JevClient()
        # Mock decide to raise or not configured, testing high-quality fallback
        with patch.object(jev, "is_configured", return_value=False):
            # 1. Chat intent
            self.assertEqual(jev.classify_steering_intent("你好呀"), "chat")
            self.assertEqual(jev.classify_steering_intent("早安！"), "chat")

            # 2. When there is a previous task
            task_state = {
                "original_goal": "编写一个快速排序算法",
                "status": "awaiting_input",
                "last_tools": [{"tool": "write", "ok": True}],
            }
            # Steering phrases
            self.assertEqual(jev.classify_steering_intent("不对，改成冒泡排序", task_state=task_state), "steering")
            self.assertEqual(jev.classify_steering_intent("不要用递归，改成迭代", task_state=task_state), "steering")
            self.assertEqual(jev.classify_steering_intent("接着刚才的代码继续写测试", task_state=task_state), "steering")

            # Independent task phrases
            self.assertEqual(jev.classify_steering_intent("帮我查一下北京天气", task_state=task_state), "independent_task")

    def test_harness_steering_injection_and_interruption(self):
        brain = MagicMock()
        jev = MagicMock()
        skill_manager = MagicMock()
        skill_manager.describe.return_value = []
        perception = MagicMock()

        harness = AgentHarness(brain, jev, self.memory, perception, skill_manager)

        session = "test:steering:session"
        self.assertFalse(harness.is_session_active(session))

        # Test injecting into inactive session returns False
        self.assertFalse(harness.inject_steering(session, "改用端口8080"))

        # Simulate run with steering injection
        # Brain yields some text then stops
        def stream_mock(messages, tools=None):
            yield ("text_delta", "正在编写代码...")
            time.sleep(0.05)
            yield ("text_delta", "已完成一部分")
            return "代码编写完毕", [], {}

        brain.stream_chat_and_tools.side_effect = stream_mock
        jev.is_configured.return_value = False

        steered_applied = []
        def on_steering(text):
            steered_applied.append(text)

        # Run harness in thread
        result_holder = []
        def run_task():
            res = harness.run(
                "请编写一个 Web 服务",
                session_id=session,
                scope="main",
                on_steering_applied=on_steering
            )
            result_holder.append(res)

        t = threading.Thread(target=run_task)
        t.start()

        # Wait until active
        for _ in range(50):
            if harness.is_session_active(session):
                break
            time.sleep(0.02)

        self.assertTrue(harness.is_session_active(session))
        # Inject steering while active!
        injected = harness.inject_steering(session, "注意：改用 aiohttp，不要用 requests")
        self.assertTrue(injected)

        t.join(timeout=5)
        self.assertFalse(harness.is_session_active(session))

        # Check history has steering message
        history, _ = self.memory.get_history(session)
        has_steering = any(
            msg.get("role") == "user" and "aiohttp" in str(msg.get("content"))
            for msg in history
        )
        self.assertTrue(has_steering)

    def test_identity_and_concurrency_in_main(self):
        app = MiniBotApp.__new__(MiniBotApp)
        app.memory = self.memory
        app.jev = JevClient()
        app.harness = MagicMock()
        app.harness.is_session_active.return_value = False
        app.dispatcher = MagicMock()
        app._stop = threading.Event()
        app.pet_widget = None

        # 1. Pet identity uses main workspace
        scope, session, channel, target, tools = app.identity(None)
        self.assertEqual(scope, "main")
        self.assertEqual(session, "main:pet")
        self.assertEqual(channel, "pet")
        self.assertTrue(tools)

        # 2. QQ Private identity uses qq_private_<uid>
        priv_event = {"message_type": "private", "user_id": 99999}
        scope, session, channel, target, _ = app.identity(priv_event)
        self.assertEqual(scope, "qq_private_99999")
        self.assertEqual(session, "qq:private:99999")

        # 3. QQ Group identity uses qq_group_<group_id>
        grp_event = {"message_type": "group", "group_id": 88888, "user_id": 12345}
        scope, session, channel, target, _ = app.identity(grp_event)
        self.assertEqual(scope, "qq_group_88888")
        self.assertEqual(session, "qq:group:88888")

        # 4. Concurrency: When session is running, non-steering intent submits to async concurrent session
        app.harness.is_session_active.return_value = True
        with patch.object(app.jev, "classify_steering_intent", return_value="independent_task"):
            app.submit_query("今天天气怎么样", custom_scope="main", custom_session="web:main:1")
            submitted_session = app.dispatcher.submit.call_args.args[0]
            # Should be an async concurrent session ID!
            self.assertTrue(":async:" in submitted_session)

        # 5. Steering: When session is running and intent is steering, injects steering directly
        app.harness.inject_steering.return_value = True
        with patch.object(app.jev, "classify_steering_intent", return_value="steering"):
            fut = app.submit_query("不对，改一下端口为9000", custom_scope="main", custom_session="web:main:1")
            self.assertIn("实时引导", fut.result())
            app.harness.inject_steering.assert_called_with("web:main:1", "不对，改一下端口为9000")

    def test_sse_broadcast_and_registry(self):
        sid = "test:sse:broadcast"
        q = register_sse_subscriber(sid)
        try:
            broadcast_sse(sid, "text_delta", {"full": "测试打字机"})
            msg = q.get_nowait()
            self.assertEqual(msg["event"], "text_delta")
            self.assertEqual(msg["data"]["full"], "测试打字机")
        finally:
            unregister_sse_subscriber(sid, q)

    def test_qq_markdown_suppression_prompt(self):
        from core.harness import build_structured_system_prompt
        # QQ channels get markdown restriction
        qq_priv_prompt = build_structured_system_prompt(scope="qq_private_123", channel="qq_private")
        self.assertIn("【QQ通道格式限制】", qq_priv_prompt)
        self.assertIn("不支持 Markdown", qq_priv_prompt)

        qq_grp_prompt = build_structured_system_prompt(scope="qq_group_456", channel="qq_group")
        self.assertIn("【QQ通道格式限制】", qq_grp_prompt)

        # Pet and Web do NOT get QQ markdown restriction
        pet_prompt = build_structured_system_prompt(scope="main", channel="pet")
        self.assertNotIn("【QQ通道格式限制】", pet_prompt)

        web_prompt = build_structured_system_prompt(scope="main", channel="web")
        self.assertNotIn("【QQ通道格式限制】", web_prompt)

    def test_code_closed_loop_and_file_verification_rules(self):
        from core.harness import build_structured_system_prompt
        prompt = build_structured_system_prompt()
        self.assertIn("【执行与交付顺序】", prompt)
        self.assertIn("失败则调用工具修复并重新测试", prompt)
        self.assertIn("全部通过后才能给出一次简短交付结果", prompt)
        self.assertNotIn("使用说明与亮点", prompt)
        self.assertIn("【文件存在性验证与反幻觉】", prompt)
        self.assertIn("capture_screen", prompt)

    def test_web_session_context_isolation(self):
        from core.decision_context import build_decision_context
        # Insert a chat message in memory for main scope
        self.memory.append_chat("main", {"role": "user", "text": "跨会话泄漏测试", "time": time.time()})
        
        # When querying context for a web session, recent_chat must be empty
        ctx = build_decision_context(self.memory, session_id="web:main:new_sess", scope="main")
        self.assertEqual(ctx["recent_chat"], [])

    def test_pet_bubble_markdown_and_image_formatting(self):
        from channels.pet_channel import convert_markdown_to_pet_html
        md = "**加粗文字** 和 *斜体*，列表：\n- 项目 A\n- 项目 B\n\n`code block`"
        html = convert_markdown_to_pet_html(md)
        self.assertIn("<span", html)
        self.assertIn("加粗文字", html)
        self.assertIn("项目 A", html)

    def test_checkpoint_manager_and_rollback(self):
        from core.checkpoint import CheckpointManager
        db_path = Path(self.temp_dir) / "checkpoints.sqlite3"
        mgr = CheckpointManager(db_path=db_path)

        # 1. Simulate file creation
        test_file = Path(self.temp_dir) / "test_script.py"
        change1 = mgr.record_file_change(
            session_id="web:main:s1",
            scope="main",
            abs_path=str(test_file),
            before_content=None,
            after_content="def hello():\n    print('v1')\n",
            message_id=1,
        )
        self.assertTrue(change1["is_new"])
        self.assertGreater(change1["added"], 0)
        test_file.write_text("def hello():\n    print('v1')\n", encoding="utf-8")

        # 2. Simulate file modification in message 2
        change2 = mgr.record_file_change(
            session_id="web:main:s1",
            scope="main",
            abs_path=str(test_file),
            before_content="def hello():\n    print('v1')\n",
            after_content="def hello():\n    print('v2 modified')\n",
            message_id=2,
        )
        self.assertFalse(change2["is_new"])
        test_file.write_text("def hello():\n    print('v2 modified')\n", encoding="utf-8")

        # 3. Add a message to memory
        self.memory.append_message("web:main:s1", {"role": "user", "content": "修改为v2"})

        # 4. Rollback to message 2 -> should revert test_file to v1 content!
        res = mgr.rollback_to_message("web:main:s1", target_message_id=2, memory=self.memory)
        self.assertTrue(res["success"])
        self.assertEqual(test_file.read_text(encoding="utf-8"), "def hello():\n    print('v1')\n")

        # 5. Rollback to message 1 -> should delete test_file because before_content was None!
        res2 = mgr.rollback_to_message("web:main:s1", target_message_id=1, memory=self.memory)
        self.assertTrue(res2["success"])
        self.assertFalse(test_file.exists())

    def test_turn_tracking_catches_skill_written_media(self):
        """技能直接写盘的 mp4/gif/图片也必须能被回滚删掉，被覆盖的旧媒体要能还原。"""
        from core.checkpoint import CheckpointManager

        scope = "ckpt_turn_media_test"
        ws = workspace_for(scope)
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True, exist_ok=True)
        try:
            (ws / "old.mp4").write_bytes(b"\x00" * 64)
            (ws / "keep.txt").write_text("keep", encoding="utf-8")

            mgr = CheckpointManager(db_path=Path(self.temp_dir) / "ckpt.sqlite3")
            session = f"web:{scope}:s1"
            msg_id = self.memory.append_message(session, {"role": "user", "content": "做个动画"})["_id"]

            mgr.begin_turn(session, scope, message_id=msg_id)
            # 模拟 execute_skill / ffmpeg 绕过 write 直接落盘
            (ws / "anim.mp4").write_bytes(b"m" * 256)
            (ws / "preview.gif").write_bytes(b"g" * 128)
            (ws / "images").mkdir()
            (ws / "images" / "img_1.png").write_bytes(b"p" * 32)
            (ws / "old.mp4").write_bytes(b"o" * 512)
            stats = mgr.end_turn(session)
            self.assertEqual(stats["created"], 3)
            self.assertEqual(stats["modified"], 1)

            res = mgr.rollback_to_message(session, msg_id, self.memory, scope=scope)
            actions = {f["path"]: f["action"] for f in res["reverted_files"]}
            self.assertEqual(actions.get("anim.mp4"), "deleted")
            self.assertEqual(actions.get("preview.gif"), "deleted")
            self.assertEqual(actions.get("images/img_1.png"), "deleted")
            self.assertEqual(actions.get("old.mp4"), "restored")

            self.assertFalse((ws / "anim.mp4").exists())
            self.assertFalse((ws / "preview.gif").exists())
            self.assertFalse((ws / "images").exists(), "本轮新建的空目录也要收掉")
            self.assertEqual((ws / "old.mp4").stat().st_size, 64)
            self.assertEqual((ws / "keep.txt").read_text(encoding="utf-8"), "keep")
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_edit_tool(self):
        brain = MagicMock()
        jev = MagicMock()
        skill_manager = MagicMock()
        perception = MagicMock()
        harness = AgentHarness(brain, jev, self.memory, perception, skill_manager)

        test_file = Path(self.temp_dir) / "sample_edit.py"
        test_file.write_text("def run():\n    a = 1\n    return a\n", encoding="utf-8")

        # Execute edit
        res = harness._execute_tool(
            "edit",
            {"file_path": str(test_file), "target": "a = 1", "replacement": "a = 999"},
            scope="main",
            session_id="web:main:edit_test",
            channel="web",
            target="",
            permitted={"edit"},
        )
        self.assertTrue(res["ok"])
        self.assertIn("a = 999", test_file.read_text(encoding="utf-8"))
        self.assertGreater(res["added"], 0)

    def test_submit_query_accepts_input_images(self):
        # Verify MiniBotApp.submit_query accepts input_images without TypeError
        app = MiniBotApp.__new__(MiniBotApp)
        app._stop = MagicMock()
        app._stop.is_set.return_value = False
        app.harness = MagicMock()
        app.harness.is_session_active.return_value = False
        app.memory = self.memory
        app.jev = MagicMock()
        app.jev.classify_steering_intent.return_value = "chat"
        app.dispatcher = MagicMock()
        app.pet_widget = None

        fake_future = MagicMock()
        app.dispatcher.submit.return_value = fake_future

        # Calling with input_images keyword argument should succeed!
        res_future = app.submit_query(
            "测试任务",
            custom_scope="main",
            custom_session="web:main:img_test",
            input_images=["data:image/png;base64,iVBORw0KGgo="],
        )
        self.assertEqual(res_future, fake_future)
        app.dispatcher.submit.assert_called_once()
        args, kwargs = app.dispatcher.submit.call_args
        self.assertEqual(args[8], ["data:image/png;base64,iVBORw0KGgo="])

    def test_session_auto_rename_and_listing(self):
        # 1. Create a session in task_states with default placeholder title
        sid = "web:main:session_test_1"
        self.memory.save_task_state(sid, {
            "original_goal": "新对话",
            "scope": "main"
        })
        with self.memory._get_conn() as db:
            db.execute("INSERT OR REPLACE INTO sessions_v2 VALUES (?,?)", (sid, time.time()))

        # Add a user message
        self.memory.append_message(sid, {"role": "user", "content": "帮我写一个贪吃蛇游戏\n用python写"})

        # Query sessions using the logic in webui/server.py
        with self.memory._get_conn() as db:
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
                ("web:main%", "web:main%", "web:main%")
            ).fetchall()
            found_sids = [r["session_id"] for r in rows]
            self.assertIn(sid, found_sids)

            # Auto rename logic check
            state = self.memory.get_task_state(sid) or {}
            title = state.get("original_goal", "")
            if not title or title in ("新对话", "新任务对话", "默认会话"):
                msg_rows = db.execute(
                    "SELECT payload FROM messages_v2 WHERE session_id=? ORDER BY id ASC LIMIT 5",
                    (sid,)
                ).fetchall()
                for m in msg_rows:
                    p = json.loads(m["payload"])
                    if p.get("role") == "user" and p.get("content"):
                        c = p["content"].strip().split("\n")[0].strip()
                        title = (c[:24] + "...") if len(c) > 24 else c
                        state["original_goal"] = title
                        self.memory.save_task_state(sid, state)
                        break

            self.assertEqual(title, "帮我写一个贪吃蛇游戏")
            updated_state = self.memory.get_task_state(sid)
            self.assertEqual(updated_state["original_goal"], "帮我写一个贪吃蛇游戏")

    def test_http_server_connection_abort_handling(self):
        from webui.server import ConfigServerHandler, ConfigHTTPServer
        # 1. Test handle_one_request suppresses ConnectionAbortedError / ConnectionResetError
        handler = ConfigServerHandler.__new__(ConfigServerHandler)
        handler.close_connection = False

        with patch("http.server.BaseHTTPRequestHandler.handle_one_request", side_effect=ConnectionAbortedError(10053, "Aborted")):
            handler.handle_one_request()
            self.assertTrue(handler.close_connection)

        # 2. Test ConfigHTTPServer.handle_error suppresses 10053/10054
        server = ConfigHTTPServer.__new__(ConfigHTTPServer)
        with patch("sys.exc_info", return_value=(ConnectionAbortedError, ConnectionAbortedError(10053, "Aborted"), None)):
            with patch("socketserver.BaseServer.handle_error") as mock_super_err:
                server.handle_error(MagicMock(), ("127.0.0.1", 12345))
                mock_super_err.assert_not_called()


    def test_live_tool_delta_streaming_and_sse_dispatch(self):
        # 1. Verify streaming parser yields dynamic line counts
        from core.streaming_parser import parse_tool_delta
        delta_res = parse_tool_delta("write", '{"file_path": "pelican.html", "content": "<div>Pelican</div>\n<span>Test</span>\n', scope="main")
        self.assertTrue(delta_res["is_file_op"])
        self.assertEqual(delta_res["file_path"], "pelican.html")
        self.assertEqual(delta_res["lines_added"], 3)
        self.assertIn("Pelican", delta_res["snippet"])

        # 2. Verify Harness on_tool_delta callback invocation
        brain = MagicMock()
        brain.stream_chat_and_tools.return_value = iter([
            ("tool_call_delta", {
                "index": 0,
                "id": "call_123",
                "name": "write",
                "delta": "<div>Pelican</div>\n",
                "arguments_so_far": '{"file_path": "pelican.html", "content": "<div>Pelican</div>\n"}'
            }),
            ("text_delta", "代码已写好。")
        ])
        jev = MagicMock()
        jev.is_configured.return_value = False
        skill_manager = MagicMock()
        skill_manager.describe.return_value = []
        perception = MagicMock()

        harness = AgentHarness(brain, jev, self.memory, perception, skill_manager)
        received_deltas = []

        def mock_tool_delta(info):
            received_deltas.append(info)

        harness._run(
            "帮我写一个网页",
            session_id="test:streaming:session",
            scope="main",
            on_tool_delta=mock_tool_delta
        )

        self.assertEqual(len(received_deltas), 1)
        first_delta = received_deltas[0]
        self.assertEqual(first_delta["name"], "write")
        self.assertTrue(first_delta["is_file_op"])
        self.assertEqual(first_delta["file_path"], "pelican.html")
        self.assertEqual(first_delta["lines_added"], 2)

        # 3. Verify SSE dispatch
        q = register_sse_subscriber("test:sse:session")
        try:
            broadcast_sse("test:sse:session", "tool_delta", first_delta)
            msg = q.get_nowait()
            self.assertEqual(msg["event"], "tool_delta")
            self.assertEqual(msg["data"]["file_path"], "pelican.html")
            self.assertEqual(msg["data"]["lines_added"], 2)
        finally:
            unregister_sse_subscriber("test:sse:session", q)



    def test_vlm_multi_model_config(self):
        """Test multi-model configuration helpers in core/config.py."""
        from core.config import get_vlm_models, resolve_vlm_model_config

        # 1. Test with custom multi-model config (no preset defaults)
        custom_cfg = {
            "vlm": {
                "base_url": "https://api.openai.com/v1",
                "model": "model-a",
                "enable_thinking": False,
                "reasoning_effort": "none",
                "models": [
                    {"id": "model-a", "name": "Model Alpha", "model": "model-a", "is_default": True},
                    {"id": "model-b", "name": "Model Beta", "model": "model-b", "enable_thinking": True, "reasoning_effort": "high"}
                ]
            }
        }
        models = get_vlm_models(custom_cfg)
        self.assertEqual(len(models), 2)
        default_model = next((m for m in models if m.get("is_default")), None)
        self.assertIsNotNone(default_model)
        self.assertEqual(default_model["id"], "model-a")

        # Resolve model-b with thinking
        resolved_b = resolve_vlm_model_config("model-b", custom_cfg)
        self.assertEqual(resolved_b["id"], "model-b")
        self.assertTrue(resolved_b["enable_thinking"])
        self.assertEqual(resolved_b["reasoning_effort"], "high")

        # Fallback to default when invalid ID passed
        fallback = resolve_vlm_model_config("nonexistent-xyz", custom_cfg)
        self.assertEqual(fallback["id"], "model-a")

        # 2. Test single model without models array (no preset fake models)
        single_cfg = {
            "vlm": {
                "base_url": "https://api.openai.com/v1",
                "model": "my-only-model",
                "models": []
            }
        }
        single_models = get_vlm_models(single_cfg)
        self.assertEqual(len(single_models), 1)
        self.assertEqual(single_models[0]["model"], "my-only-model")
        self.assertTrue(single_models[0]["is_default"])

    def test_pet_and_qq_channel_enforce_primary_model(self):
        """Verify Pet and QQ channels force the primary model while Web allows custom selection."""
        app = MiniBotApp.__new__(MiniBotApp)
        app.dispatcher = MagicMock()
        app.harness = MagicMock()
        app.memory = self.memory
        app.identity = MagicMock(return_value=("main", "main:pet", "pet", "", True))
        app._handle_user_query = MagicMock()

        # 1. Pet channel: model override is ignored, forced to None (primary model)
        app.submit_query("你好", custom_session="pet:main", model="deepseek-r1")
        args, kwargs = app.dispatcher.submit.call_args
        self.assertIsNone(kwargs.get("model_override"))

        # 2. QQ channel: model override is ignored, forced to None (primary model)
        app.identity = MagicMock(return_value=("qq_priv", "qq:private:123", "qq_private", "123", True))
        app.submit_query("你好", model="deepseek-r1")
        args, kwargs = app.dispatcher.submit.call_args
        self.assertIsNone(kwargs.get("model_override"))

        # 3. Web channel: model override is preserved
        app.identity = MagicMock(return_value=("main", "web:main", "web", "", True))
        app.submit_query("写个代码", custom_session="web:main", model="deepseek-r1")
        args, kwargs = app.dispatcher.submit.call_args
        self.assertEqual(kwargs.get("model_override"), "deepseek-r1")

    def test_thinking_process_streaming_and_completion(self):
        """Verify thinking reasoning_delta streams and triggers completion before text output."""
        brain = MagicMock()
        brain.stream_chat_and_tools.return_value = iter([
            ("reasoning_delta", "正在分析需求..."),
            ("reasoning_delta", "需要使用 Canvas 绘制小球。"),
            ("text_delta", "小球动画已准备完成。")
        ])
        jev = MagicMock()
        jev.is_configured.return_value = False
        skill_manager = MagicMock()
        skill_manager.describe.return_value = []
        perception = MagicMock()

        harness = AgentHarness(brain, jev, self.memory, perception, skill_manager)
        reasoning_chunks = []
        reasoning_completed = []
        text_chunks = []

        def on_reasoning(delta, full):
            reasoning_chunks.append(delta)

        def on_reasoning_done(full):
            reasoning_completed.append(full)

        def on_text(delta, full):
            text_chunks.append(delta)

        harness._run(
            "画个球",
            session_id="test:thinking:session",
            scope="main",
            on_reasoning_chunk=on_reasoning,
            on_reasoning_complete=on_reasoning_done,
            on_text_chunk=on_text
        )

        self.assertEqual(len(reasoning_chunks), 2)
        self.assertEqual(reasoning_chunks[0], "正在分析需求...")
        # Auto-completion triggered when text_delta arrived
        self.assertEqual(len(reasoning_completed), 1)
        self.assertIn("需要使用 Canvas 绘制小球。", reasoning_completed[0])
        self.assertEqual(len(text_chunks), 1)
        self.assertEqual(text_chunks[0], "小球动画已准备完成。")

    def test_submit_query_forwards_stream_reset_callback(self):
        """草稿作废回调必须一路传到 Harness，否则前端没法把旧文本标成作废。"""
        app = MiniBotApp.__new__(MiniBotApp)
        app.dispatcher = MagicMock()
        app.harness = MagicMock()
        app.memory = self.memory
        app.jev = None
        app.identity = MagicMock(return_value=("main", "web:main", "web", "", True))
        reset = MagicMock()
        app.submit_query("继续修第17步", custom_scope="main", custom_session="web:main", on_text_reset=reset)
        kwargs = app.dispatcher.submit.call_args.kwargs
        self.assertIs(kwargs.get("on_text_reset"), reset)

    def test_text_deltas_stream_live_even_before_tool_calls(self):
        """普通文本要边生成边推送：不能等整轮结束，也不能把工具前的说明藏起来。"""
        calls = []
        streamed = []
        tool_saw_text = []

        def stream(messages, tools=None):
            calls.append(1)
            if len(calls) == 1:
                yield 'text_delta', '先算'
                yield 'text_delta', '一下旋转角：'
                yield 'text_delta', '重新解算。'
                return '', [{'id': 'c1', 'type': 'function', 'function': {
                    'name': 'bash', 'arguments': json.dumps({'command': 'echo hi'})}}], {}
            yield 'text_delta', '修好了。'
            return '', [], {}

        brain = MagicMock()
        brain.stream_chat_and_tools.side_effect = stream
        jev = MagicMock()
        jev.is_configured.return_value = False
        skill_manager = MagicMock()
        skill_manager.describe.return_value = []
        harness = AgentHarness(brain, jev, self.memory, MagicMock(), skill_manager)

        def dispatch(name, args, *extra, **kwargs):
            # 工具还没执行时，界面就应该已经拿到这段进度文本了。
            tool_saw_text.append(list(streamed))
            return {'ok': True, 'exit_code': 0, 'output': 'hi'}

        with patch.object(harness, '_dispatch_tool', side_effect=dispatch):
            reply = harness.run(
                '算一下腿的角度再修',
                on_text_chunk=lambda delta, full: streamed.append(full),
                session_id='web:main:stream',
                scope='main',
                channel='web',
            )

        self.assertTrue(tool_saw_text and tool_saw_text[0], '工具执行前必须已经推送过文本')
        self.assertIn('先算一下旋转角：重新解算。', streamed)
        self.assertEqual('修好了。', streamed[-1])
        self.assertEqual('修好了。', reply)



    def test_workspace_tool_permissions_in_web_channel(self):
        """Verify user workspaces like 'my_project' in Web channel are trusted and not blocked by QQ permissions."""
        from core.qq_authority import tool_access
        # 1. tool_access check
        self.assertTrue(tool_access("my_project", "write", {"file_path": "demo.html"}))
        self.assertTrue(tool_access("my_project", "bash", {"command": "dir"}))

        # 2. Harness tool execution check
        brain = MagicMock()
        jev = MagicMock()
        skill_manager = MagicMock()
        skill_manager.describe.return_value = []
        perception = MagicMock()

        harness = AgentHarness(brain, jev, self.memory, perception, skill_manager)
        res = harness._execute_tool(
            "write",
            {"file_path": "pelican_bike.html", "content": "<h1>test</h1>"},
            scope="my_project",
            session_id="web:my_project:default",
            channel="web"
        )
        self.assertTrue(res.get("ok"), f"Expected ok=True but got {res}")
        self.assertNotEqual(res.get("error_code"), "PERMISSION_DENIED")

    def test_active_turn_buffer_and_history_reasoning(self):
        """Verify active turns track streaming state and message history preserves reasoning_content."""
        from webui.server import _active_session_turns, _active_turns_lock

        sid = "web:test_ws:s1"
        with _active_turns_lock:
            _active_session_turns[sid] = {
                "session_id": sid,
                "scope": "test_ws",
                "status": "正在深度思考...",
                "reasoning": "正在分析问题...",
                "reasoning_done": False,
                "text": "初始文本",
                "tools": [{"name": "write", "status": "done"}],
                "started_at": 12345,
                "is_active": True
            }

        with _active_turns_lock:
            turn = _active_session_turns.get(sid)
            self.assertIsNotNone(turn)
            self.assertEqual(turn["reasoning"], "正在分析问题...")
            self.assertEqual(turn["status"], "正在深度思考...")
            _active_session_turns.pop(sid, None)

        # Message persistence check
        saved = self.memory.append_message(sid, {
            "role": "assistant",
            "content": "最终回答",
            "reasoning_content": "完整深度思考步骤"
        })
        history, _ = self.memory.get_history(sid)
        found = next((m for m in history if m.get("_id") == saved["_id"]), None)
        self.assertIsNotNone(found)
        self.assertEqual(found.get("reasoning_content"), "完整深度思考步骤")

    def test_model_and_thinking_override(self):
        """Verify web query submits custom model and thinking effort override."""
        from core.brain import VLMBrain

        brain = VLMBrain()
        # 1. No thinking override
        s1 = brain._settings({
            "id": "gemma-4-26b-thinking",
            "enable_thinking": False,
            "reasoning_effort": "none"
        })
        self.assertFalse(s1[3].get("enable_thinking"))
        self.assertEqual(s1[3].get("reasoning_effort"), "none")

        # 2. Thinking enabled override
        s2 = brain._settings({
            "id": "gemma-4-26b-thinking",
            "enable_thinking": True,
            "reasoning_effort": "high"
        })
        self.assertTrue(s2[3].get("enable_thinking"))
        self.assertEqual(s2[3].get("reasoning_effort"), "high")



    def test_harness_direct_retry_on_stream_error(self):
        """Verify harness directly retries once without compressing context on stream error."""
        from core.harness import AgentHarness
        from core.compaction import ContextCompactor

        mock_brain = MagicMock()
        attempt = 0

        def stream_gen(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                # Yield error
                yield ("error", "SSLEOFError: Connection closed by peer")
            else:
                yield ("text_delta", "重试成功回答")

        mock_brain.stream_chat_and_tools.side_effect = stream_gen
        mock_brain.chat.return_value = "总结"

        mock_skills = MagicMock()
        mock_skills.describe.return_value = []
        mock_skills.catalog.return_value = []
        mock_jev = MagicMock()
        mock_jev.is_configured.return_value = False
        harness = AgentHarness(
            brain=mock_brain,
            jev=mock_jev,
            memory=self.memory,
            perception=MagicMock(),
            skill_manager=mock_skills
        )
        with patch.object(ContextCompactor, "_run_compaction_worker") as mock_compact, \
             patch.object(harness, "review_completion", return_value=(True, "")):
            res = harness.run("测试重试", session_id="test:retry:session", scope="main")
            self.assertEqual(res, "重试成功回答")
            self.assertEqual(attempt, 2)
            # Compaction must NOT be called on error retry
            mock_compact.assert_not_called()

    def test_session_compact_and_clear_logic(self):
        """Verify /cc compaction and /clear session cleanup functionality."""
        from core.compaction import ContextCompactor

        sid = "web:main:test_compact"
        # Seed messages
        for i in range(12):
            self.memory.append_message(sid, {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"Message content {i}"
            })

        history, summary = self.memory.get_history(sid)
        self.assertEqual(len(history), 12)
        self.assertIsNone(summary)

        # Test compaction with keep=4
        mock_brain = MagicMock()
        mock_brain.chat.return_value = "checkpoint summary"
        compactor = ContextCompactor(brain=mock_brain, memory=self.memory, session_id=sid)
        ok = compactor._run_compaction_worker(history, keep=4)
        self.assertTrue(ok)

        # After compaction, summary exists and covered messages archived
        history_after, summary_after = self.memory.get_history(sid)
        self.assertIsNotNone(summary_after)
        self.assertGreater(summary_after["covered_until_id"], 0)
        self.assertLess(len(history_after), 12)

        # Test clear
        with self.memory._get_conn() as db:
            for table in ("messages_v2", "archived_messages_v2", "summaries_v2"):
                db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
        self.memory.save_task_state(sid, {
            "original_goal": "新对话",
            "status": "awaiting_input",
            "scope": "main"
        })

        cleared_history, cleared_summary = self.memory.get_history(sid)
        self.assertEqual(len(cleared_history), 0)
        self.assertIsNone(cleared_summary)
        cleared_state = self.memory.get_task_state(sid)
        self.assertEqual(cleared_state["original_goal"], "新对话")

if __name__ == "__main__":
    unittest.main()



class SteeringBlockTests(unittest.TestCase):
    """引导必须插在生效位置，而不是堆在整轮末尾。"""

    def test_active_turn_records_steering_block_in_order(self):
        from webui.server import (
            _turn_start_block, _turn_end_block, _turn_reset,
            _active_session_turns, _active_turns_lock,
        )

        sid = "web:test_steer:order"
        _turn_reset(sid)
        with _active_turns_lock:
            _active_session_turns[sid] = {"session_id": sid, "blocks": []}
        try:
            with _active_turns_lock:
                _turn_start_block(sid, "reasoning")["text"] = "先看现状"
                _turn_start_block(sid, "text")["text"] = "旧草稿"
                _turn_end_block(sid, "text")
                block = _turn_start_block(sid, "steering")
                block["text"] = "别改标题，只改颜色"
                _turn_end_block(sid, "steering")
                _turn_start_block(sid, "text")["text"] = "按引导重写"
            types = [b["type"] for b in _active_session_turns[sid]["blocks"]]
            self.assertEqual(types, ["reasoning", "text", "steering", "text"])
            self.assertEqual(_active_session_turns[sid]["blocks"][2]["text"], "别改标题，只改颜色")
        finally:
            _turn_reset(sid)

    def test_history_steering_is_not_duplicated_while_turn_is_active(self):
        from webui.server import _drop_duplicated_steerings

        history = [
            {"role": "user", "content": "开始改"},
            {"role": "user", "is_steering": True,
             "content": "【用户实时引导与方向修正】\n别改标题，只改颜色\n（注意：…）"},
            {"role": "assistant", "content": "旧草稿"},
        ]
        turn = {"blocks": [{"type": "steering", "text": "别改标题，只改颜色", "open": False}]}
        kept = _drop_duplicated_steerings(history, turn)
        self.assertEqual([m.get("content") for m in kept], ["开始改", "旧草稿"])

    def test_history_steering_kept_without_active_turn(self):
        from webui.server import _drop_duplicated_steerings

        history = [{"role": "user", "is_steering": True, "content": "补充引导：别改标题"}]
        self.assertEqual(len(_drop_duplicated_steerings(history, None)), 1)
        self.assertEqual(len(_drop_duplicated_steerings(history, {"blocks": []})), 1)

    def test_qq_owner_events_sync_to_pet_and_web(self):
        """Verify QQ owner message & stream replies trigger Pet bubble/action and broadcast to Web main."""
        from unittest.mock import patch, MagicMock
        from main import MiniBotApp

        app = MiniBotApp.__new__(MiniBotApp)
        app._stop = MagicMock()
        app._stop.is_set.return_value = False
        from core.memory import MiniMemory
        import tempfile
        t_dir = tempfile.mkdtemp()
        app.memory = MiniMemory(str(Path(t_dir) / "test.db"))
        app.pet_widget = MagicMock()
        app.harness = MagicMock()

        # 模拟 Harness 执行时依次触发各个回调
        def fake_harness_run(text, on_text_chunk=None, on_status_change=None, **kwargs):
            # harness.run 现在的入参全是关键字：记忆/尾部上下文/会话身份分开传，
            # 不再有第二个位置参数 context。
            on_text, status = on_text_chunk, on_status_change
            status("正在深度思考...")
            on_reasoning = kwargs.get("on_reasoning_chunk")
            if on_reasoning:
                on_reasoning("思考1", "思考1")
            on_reasoning_done = kwargs.get("on_reasoning_complete")
            if on_reasoning_done:
                on_reasoning_done("思考1完成")

            status("正在执行工具...")
            on_tool_call = kwargs.get("on_tool_call")
            if on_tool_call:
                on_tool_call("write", {"path": "test.txt"})
            on_tool_delta = kwargs.get("on_tool_delta")
            if on_tool_delta:
                on_tool_delta({"output": "chunk1"})
            on_tool_result = kwargs.get("on_tool_result")
            if on_tool_result:
                on_tool_result("write", {"ok": True})

            on_text("你好", "你好")
            on_text("，主人！", "你好，主人！")
            return "你好，主人！"

        app.harness.run = fake_harness_run

        broadcast_events = []
        sync_messages = []

        import copy
        from core.config import load_config as real_load_config
        base_cfg = copy.deepcopy(real_load_config())
        base_cfg.setdefault("channels", {}).setdefault("napcat", {})["owner_ids"] = ["416448943"]

        with patch("webui.server.broadcast_to_web_main", side_effect=lambda evt, data: broadcast_events.append((evt, data))), \
             patch("webui.server.sync_message_to_web_main", side_effect=lambda app, role, content, *args: sync_messages.append((role, content))), \
             patch("core.config.load_config", return_value=base_cfg):

            event = {"user_id": 416448943, "message_type": "private", "raw_message": "做个视频"}
            reply = app._handle_user_query(
                "做个视频",
                scope="main",
                session="qq:private:416448943",
                channel="qq_private",
                target="416448943",
                tools=True,
                qq_event=event,
            )

            # 1. 验证回复内容
            self.assertEqual(reply, "你好，主人！")

            # 2. 验证 Pet 端接收到了思考动作、任务状态、流式打字与完成播报
            app.pet_widget.set_action.assert_called_with(6, duration_ms=4000)
            app.pet_widget.start_task_safe.assert_any_call("做个视频", "正在思考…")
            app.pet_widget.set_task_safe.assert_any_call("做个视频", "正在深度思考...")
            app.pet_widget.set_task_safe.assert_any_call("做个视频", "正在执行工具...")
            # 工具调用要在正文回显活动行（与 Web 工具块一致），结果打 ✅
            app.pet_widget.append_activity_safe.assert_any_call("写入了文件 · test.txt")
            app.pet_widget.complete_activity_safe.assert_any_call(True)
            app.pet_widget.stream_say_safe.assert_any_call("你好")
            app.pet_widget.stream_say_safe.assert_any_call("你好，主人！")
            app.pet_widget.finish_say_safe.assert_called_with("你好，主人！", 3)

            # 3. 验证 Web 端收到了用户消息同步和最终回复同步
            self.assertTrue(any(r == "user" and "[QQ主人]" in c for r, c in sync_messages))
            self.assertTrue(any(r == "assistant" and "你好，主人！" in c for r, c in sync_messages))

            # 4. 验证 Web 端收到了完整的实时事件广播：status、reasoning、tool、text_delta
            event_types = [e[0] for e in broadcast_events]
            self.assertIn("status", event_types)
            self.assertIn("reasoning_delta", event_types)
            self.assertIn("reasoning_complete", event_types)
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_delta", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("text_delta", event_types)
