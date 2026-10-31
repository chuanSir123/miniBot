import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from core import config
from core.memory import MiniMemory
from core.compaction import ContextCompactor
from core.harness import AgentHarness
from core.proactive import ProactiveEngine
from core.runtime import Dispatcher
from core.skill_manager import SkillManager
from core.process import run_bounded


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(config, "CONFIG_FILE_PATH", self.root / "config.json")
        self.patch.start()
        self.media_patch = patch("core.media_store.MEDIA_ROOT", self.root / "media")
        self.media_patch.start()
        self.workspace_patch = patch("core.workspace.WORKSPACE_ROOT", self.root / "workspace")
        self.workspace_patch.start()
        # 表情包库同样要隔离：否则跑测试会扫真实 assets/memes 并往 data/memes.db
        # 里写打标结果，还会真的去调 VLM。
        self.meme_db_patch = patch("core.meme.DB_PATH", self.root / "memes.db")
        self.meme_db_patch.start()
        self.meme_dir_patch = patch("core.meme.DEFAULT_MEME_DIR", self.root / "memes")
        self.meme_dir_patch.start()
        from core import meme as meme_module

        meme_module._instance = None
        config._cache = None
        config._stamp = None
        self.memory = MiniMemory(self.root / "memory.db")

    def tearDown(self):
        from core import meme as meme_module

        meme_module._instance = None
        self.meme_dir_patch.stop()
        self.meme_db_patch.stop()
        self.workspace_patch.stop()
        self.media_patch.stop()
        self.patch.stop()
        config._cache = None
        config._stamp = None
        self.temp.cleanup()


class MemoryTests(Fixture):
    def test_scopes_and_explicit_facts(self):
        self.memory.set_fact("name", "owner")
        self.memory.set_fact("name", "guest", scope="qq:1")
        self.assertEqual(self.memory.get_all_facts(), {"name": "owner"})
        self.assertEqual(self.memory.get_all_facts("qq:1"), {"name": "guest"})
        self.assertFalse(self.memory.auto_extract_facts_from_text("他说我的名字是张三"))
        self.assertTrue(
            self.memory.auto_extract_facts_from_text("记住我的名字是张三", "qq:2")
        )
        self.assertNotIn("名字", self.memory.get_all_facts())

    def test_chinese_retrieval_and_exclusion(self):
        old = self.memory.add_event("上周修复了数据库连接超时", scope="qq:1")
        current = self.memory.add_event("数据库问题", scope="qq:1")
        self.memory.add_event("数据库 secret", scope="qq:2")
        hits = self.memory.search_events_with_decay(
            "之前的数据库连接怎么修复", scope="qq:1", exclude_id=current
        )
        self.assertEqual([h["id"] for h in hits], [old])

    def test_reminders_claim_ack_retry(self):
        for i in range(2):
            self.memory.add_delayed_intent(0, str(i))
        tasks = self.memory.claim_due_intents()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(self.memory.claim_due_intents(), [])
        self.memory.finish_intent(tasks[0]["id"], True)
        self.memory.finish_intent(tasks[1]["id"], False, "offline")
        self.assertEqual(
            {t["status"] for t in self.memory.list_intents()}, {"delivered", "pending"}
        )

    def test_lease_recovery_and_cancel_isolation(self):
        task = self.memory.add_delayed_intent(0, "test", scope="qq:1", session_id="s")
        self.assertFalse(self.memory.update_intent(task, "qq:2", "s", cancel=True))
        self.assertEqual(len(self.memory.claim_due_intents(lease_sec=-1)), 1)
        self.assertEqual(len(self.memory.claim_due_intents()), 1)
        self.memory.finish_intent(task, False, defer_sec=0)
        self.assertTrue(self.memory.update_intent(task, "qq:1", "s", cancel=True))
        self.assertEqual(self.memory.claim_due_intents(), [])

    def test_dedupe_and_conditions(self):
        a = self.memory.add_delayed_intent(0, "test", dedupe_key="same")
        b = self.memory.add_delayed_intent(0, "test", dedupe_key="same")
        self.assertEqual(a, b)
        state = {"idle_sec": 70, "has_error": True, "title": "editor"}
        self.assertTrue(ProactiveEngine.condition_met("idle", state))
        self.assertFalse(ProactiveEngine.condition_met("error_cleared", state))
        self.assertTrue(ProactiveEngine.condition_met("window:edit", state))
        self.assertFalse(ProactiveEngine.condition_met("unknown", state))

    def test_maintenance_fts_consistency(self):
        for i in range(15):
            self.memory.add_event("数据库 " + str(i))
        self.memory.maintenance(max_events=2)
        self.assertEqual(len(self.memory.get_recent_events(10)), 2)
        self.assertEqual(len(self.memory.search_events_with_decay("数据库", 10)), 2)

    def test_legacy_quarantined(self):
        with self.memory._get_conn() as db:
            db.execute("CREATE TABLE core_facts(key TEXT,value TEXT)")
            db.execute("INSERT INTO core_facts VALUES ('secret','legacy')")
        self.assertNotIn("secret", MiniMemory(self.root / "memory.db").get_all_facts())


class CompactionTests(Fixture):
    def test_preserve_goal_new_messages_and_tool_groups(self):
        class Brain:
            def chat(self, messages):
                return "progress"

        history = []

        def add(m):
            history.append(self.memory.append_message("a", m))

        add({"role": "user", "content": "ORIGINAL_GOAL"})
        add(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        add({"role": "tool", "tool_call_id": "call1", "content": "result"})
        for i in range(5):
            add({"role": "user", "content": "turn " + str(i)})
            add({"role": "assistant", "content": "reply"})
        compactor = ContextCompactor(Brain(), self.memory, "a")
        compactor._run_compaction_worker(copy.deepcopy(history))
        add({"role": "user", "content": "NEW_AFTER_SNAPSHOT"})
        messages = compactor.align_and_assemble_messages(
            "system", history, "PERSISTENT_CONTEXT"
        )
        text = json.dumps(messages)
        self.assertIn("ORIGINAL_GOAL", text)
        self.assertIn("NEW_AFTER_SNAPSHOT", text)
        self.assertIn("PERSISTENT_CONTEXT", text)
        self.assertNotIn("ORIGINAL_GOAL", json.dumps(self.memory.get_history("b")))
        for i, m in enumerate(messages):
            if m["role"] == "tool":
                self.assertTrue(
                    any(
                        c.get("id") == m["tool_call_id"]
                        for a in messages[:i]
                        for c in a.get("tool_calls", [])
                    )
                )
        self.assertIn("NEW_AFTER_SNAPSHOT", json.dumps(self.memory.get_history("a")))

    def test_failed_compaction_does_not_drop(self):
        class Brain:
            def chat(self, messages):
                return "【VLM 网络异常】offline"

        history = [
            self.memory.append_message("a", {"role": "user", "content": str(i)})
            for i in range(32)
        ]
        c = ContextCompactor(Brain(), self.memory, "a")
        c._run_compaction_worker(history)
        self.assertEqual(len(self.memory.get_history("a")[0]), 32)


class FakePerception:
    def __init__(self):
        self.state = {
            "title": "editor",
            "process": "editor.exe",
            "idle_sec": 40,
            "locked": False,
            "fullscreen": False,
            "meeting": False,
        }
        self.clip = None

    def snapshot(self, **kwargs):
        return dict(self.state)

    def check_clipboard_change(self):
        result = self.clip
        self.clip = None
        return result

    def capture_screen_jpeg(self):
        return b"JPEG_TEST_BYTES"


class FakeJev:
    def is_configured(self):
        return False


class HarnessTests(Fixture):
    def test_skill_json_string_is_normalized_and_invalid_values_rejected(self):
        harness = self.harness(None)
        for value in ({"prompt": "猫娘"}, json.dumps({"prompt": "猫娘"})):
            with patch.object(harness.skill_manager, "execute_skill", return_value={"ok": True}) as execute:
                result = harness._dispatch_tool("execute_skill", {"skill_name": "image_gen", "arguments": value})
            self.assertTrue(result["ok"])
            self.assertEqual(execute.call_args.args[1], {"prompt": "猫娘"})
        for value in ('[]', 'null', 'broken', '"text"'):
            with patch.object(harness.skill_manager, "execute_skill") as execute:
                result = harness._dispatch_tool("execute_skill", {"skill_name": "image_gen", "arguments": value})
            self.assertFalse(result["ok"])
            execute.assert_not_called()
        with patch.object(harness.skill_manager, "execute_skill") as execute:
            result = harness._dispatch_tool("execute_skill", {"skill_name": "image_gen", "arguments": '{}'}, permitted=set())
        self.assertFalse(result["ok"])
        execute.assert_not_called()

    def harness(self, brain):
        return AgentHarness(
            brain,
            FakeJev(),
            self.memory,
            FakePerception(),
            SkillManager(self.root / "skills"),
        )

    def test_capture_reaches_model_and_context_stays(self):
        class Brain:
            def __init__(self):
                self.calls = []

            def stream_chat_and_tools(self, messages, tools=None):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    return (
                        "",
                        [
                            {
                                "id": "c",
                                "type": "function",
                                "function": {
                                    "name": "capture_screen",
                                    "arguments": '{"reason":"see"}',
                                },
                            }
                        ],
                        {},
                    )
                yield ("text_delta", "I see it")
                return ("I see it", [], {})

        brain = Brain()
        self.assertEqual(self.harness(brain).run("look", "sticky"), "I see it")
        payload = json.dumps(brain.calls[1])
        self.assertIn("data:image/jpeg;base64,", payload)
        self.assertIn("sticky", payload)

    def test_same_read_repeated_is_blocked_and_turn_ends_honestly(self):
        """同一个文件用同样参数反复读：第 2 次提醒，第 3 次不再执行，第 6 次如实收尾。

        pelican_bike.html 连着读七遍那种空转，模型循环本身不会自我保护
        （只提供 beforeToolCall 钩子），只能由 harness 按"签名 + 文件版本"自己判断。
        """
        from core.harness import DUPLICATE_ABORT_AT

        class Brain:
            def __init__(self):
                self.calls = 0
                self.payloads = []

            def stream_chat_and_tools(self, messages, tools=None):
                self.calls += 1
                self.payloads.append(json.dumps(messages, ensure_ascii=False))
                if False:
                    yield
                return (
                    "",
                    [{
                        "id": f"c{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"file_path": "notes.txt"}),
                        },
                    }],
                    {},
                )

        brain = Brain()
        harness = self.harness(brain)
        read_result = {"ok": True, "output": "body\n", "start_line": 1,
                       "next_start_line": 2, "has_more": True}
        with patch.object(harness, "_dispatch_tool", return_value=dict(read_result)) as dispatch:
            reply = harness.run("读一下这个文件", "local:pet", scope="main")

        # 真正执行只发生两次，之后全是拦截；否则就会一直烧到 max_turns。
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(brain.calls, DUPLICATE_ABORT_AT)
        self.assertIn("空转", reply)
        self.assertTrue(any("重复调用提醒" in p for p in brain.payloads))

    def test_repeated_read_after_file_change_is_not_blocked(self):
        """改完文件再读同一个文件是正常工作流，不能按参数相同就拦。"""
        from core.harness import call_signature, guard_repeated_call

        target = self.root / "notes.txt"
        target.write_text("first", encoding="utf-8")
        seen = {}
        signature = call_signature("read_file", {"file_path": str(target)})
        for _ in range(3):
            guard_repeated_call("read_file", {"file_path": str(target)}, signature, seen, "main", 0)
        self.assertTrue(guard_repeated_call(
            "read_file", {"file_path": str(target)}, signature, seen, "main", 0)[0])

        target.write_text("second version, longer", encoding="utf-8")
        blocked, _ = guard_repeated_call(
            "read_file", {"file_path": str(target)}, signature, seen, "main", 0)
        self.assertIsNone(blocked)

    def test_side_effecting_tool_is_never_blocked_for_same_arguments(self):
        """改完代码重跑同一条测试命令，参数必然相同，拦下来会挡住正常修复流程。"""
        from core.harness import call_signature, guard_repeated_call

        seen = {}
        signature = call_signature("execute_command", {"command": "python -m pytest -q"})
        for _ in range(6):
            blocked, notice = guard_repeated_call(
                "execute_command", {"command": "python -m pytest -q"}, signature, seen, "main", 0)
            self.assertIsNone(blocked)
            self.assertEqual("", notice)

    def test_read_file_tells_where_to_continue(self):
        """读到行数/字符窗口末尾时给出续读位置：没有这个线索，模型只能原样再读一遍。"""
        from core.workspace import workspace_for
        workspace = workspace_for("main")
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "long.txt").write_text(
            "".join(f"line{i}\n" for i in range(30)), encoding="utf-8")
        harness = self.harness(None)
        first = harness._dispatch_tool("read_file", {"file_path": "long.txt", "max_lines": 10})
        self.assertEqual(first["start_line"], 1)
        self.assertEqual(first["next_start_line"], 11)
        self.assertTrue(first["has_more"])
        second = harness._dispatch_tool(
            "read_file", {"file_path": "long.txt", "start_line": 11, "max_lines": 10})
        self.assertEqual(second["next_start_line"], 21)

    def test_permission_enforced_even_model_calls_hidden_tool(self):
        h = self.harness(None)
        result = h._dispatch_tool(
            "write_file",
            {"file_path": str(self.root / "bad"), "content": "x"},
            permitted={"list_reminders"},
        )
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "bad").exists())

    def test_reminder_tool_and_identity(self):
        h = self.harness(None)
        result = h._dispatch_tool(
            "schedule_reminder",
            {"trigger_at": 0, "message": "hello"},
            "qq:private:123",
            "session",
            "qq_private",
            "123",
        )
        self.assertTrue(result["ok"])
        task = self.memory.claim_due_intents()[0]
        self.assertEqual(
            (task["scope"], task["channel"], task["target"]),
            ("qq:private:123", "qq_private", "123"),
        )
        self.assertFalse(
            h._dispatch_tool(
                "schedule_reminder",
                {"trigger_at": 0, "message": "x", "condition": "nonsense"},
            )["ok"]
        )

    def test_large_single_line_read_bounded(self):
        path = self.root / "large"
        path.write_text("x" * 1000000)
        result = self.harness(None)._dispatch_tool(
            "read_file", {"file_path": str(path)}
        )
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(result["output"]), 12000)

    def test_stream_error_never_executes_partial_tools(self):
        class Brain:
            def stream_chat_and_tools(self, messages, tools=None):
                yield ("error", "offline")
                return (
                    "",
                    [
                        {
                            "id": "c",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"file_path": str(target), "content": "bad"}
                                ),
                            },
                        }
                    ],
                    {},
                )

        target = self.root / "bad"
        self.harness(Brain()).run("test")
        self.assertFalse(target.exists())


class ProactiveTests(Fixture):
    def test_all_due_sent_without_model(self):
        for i in range(2):
            self.memory.add_delayed_intent(0, str(i))
        sent = []
        engine = ProactiveEngine(
            self.memory,
            FakePerception(),
            FakeJev(),
            None,
            None,
            lambda *a, **k: sent.append(a) or True,
        )
        engine._tick(config.load_config()["proactive"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(
            {t["status"] for t in self.memory.list_intents()}, {"delivered"}
        )

    def test_failed_delivery_remains_pending(self):
        self.memory.add_delayed_intent(0, "x")
        engine = ProactiveEngine(
            self.memory, FakePerception(), FakeJev(), None, None, lambda *a, **k: False
        )
        engine._tick(config.load_config()["proactive"])
        self.assertEqual(self.memory.list_intents()[0]["status"], "pending")

    def test_idle_no_cloud_and_error_persists(self):
        class Jev:
            def is_configured(self):
                return True

            def decide(self, *args, **kwargs):
                calls.append(args)
                return {"intervene": 0}

        calls = []
        perception = FakePerception()
        engine = ProactiveEngine(self.memory, perception, Jev(), None, None)
        cfg = config.load_config()["proactive"]
        cfg["quiet_hours"] = [0, 0]
        engine._tick(cfg)
        self.assertEqual(calls, [])
        perception.clip = "Error: broken"
        engine._tick(cfg)
        self.assertTrue(engine._last_state["has_error"])
        engine._tick(cfg)
        self.assertTrue(engine._last_state["has_error"])
        self.assertEqual(len(calls), 1)

    def test_return_transition(self):
        p = FakePerception()
        e = ProactiveEngine(self.memory, p, FakeJev(), None, None)
        p.state["idle_sec"] = 700
        e._tick(config.load_config()["proactive"])
        p.state["idle_sec"] = 1
        e._tick(config.load_config()["proactive"])
        self.assertTrue(e._last_state["returned"])


class SkillTests(Fixture):
    def test_import_does_not_execute_and_bad_path_rejected(self):
        folder = self.root / "skills"
        folder.mkdir()
        target = self.root / "pwned"
        (folder / "evil.py").write_text(
            f"from pathlib import Path\nPath({str(target)!r}).write_text('bad')\ndef execute(context): return 'ok'",
            encoding="utf-8",
        )
        manager = SkillManager(folder)
        self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            manager.register_new_skill("../escape.py", "x=1")
        with self.assertRaises(ValueError):
            manager.execute_skill("evil")
        manager.register_new_skill("new.py", "def execute(context): return 'ok'")
        # candidates 已移除：save 即注册启用（Agent Skills 目录形态）
        self.assertIn("new", manager.list_skill_names())
        self.assertTrue((folder / "new" / "SKILL.md").is_file())

    def test_learn_usage_version_rollback(self):
        manager = SkillManager(self.root / "skills")
        name = manager.learn_from_trace(
            "read config",
            [
                {
                    "tool": "read_file",
                    "arguments": {"file_path": "example"},
                    "result": {"ok": True},
                }
            ],
        )
        self.assertIn(name, manager.list_skill_names())
        result = manager.execute_skill(
            name, {}, lambda *a: {"ok": True, "output": "ok"}
        )
        self.assertTrue(result["ok"])
        observer = SkillManager(self.root / "skills")
        manager.set_workflow_status(name, False)
        self.assertNotIn(name, observer.list_skill_names())
        self.assertNotIn(name, manager.list_skill_names())
        manager.rollback(name)
        self.assertIn(name, observer.list_skill_names())
        self.assertIn(name, manager.list_skill_names())
        data = json.loads(manager.stats_path.read_text(encoding="utf-8"))
        self.assertEqual(data[name]["success"], 1)

    def test_side_effect_workflow_candidate_and_invalid_parameters(self):
        manager = SkillManager(self.root / "skills")
        name = manager.learn_from_trace(
            "write",
            [
                {
                    "tool": "write_file",
                    "arguments": {"file_path": "x", "content": "x"},
                    "result": {"ok": True},
                }
            ],
        )
        self.assertNotIn(name, manager.list_skill_names())
        from core.skill_manager import validate_args

        with self.assertRaises(ValueError):
            validate_args(
                {"required": ["port"], "properties": {"port": {"type": "integer"}}},
                {"port": "bad"},
            )

    def test_process_output_cap_and_timeout(self):
        import sys

        result = run_bounded(
            [sys.executable, "-c", "print('x'*1000000)"], max_bytes=3000
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["output"]), 3100)
        result = run_bounded(
            [sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.2
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["timeout"])


class RuntimeTests(unittest.TestCase):
    def test_session_order_capacity_and_shutdown(self):
        dispatcher = Dispatcher(2, 2)
        gate = threading.Event()
        started = threading.Event()
        seen = []

        def first():
            started.set()
            gate.wait(2)
            seen.append(1)

        a = dispatcher.submit("s", first)
        self.assertTrue(started.wait(1))
        b = dispatcher.submit("s", lambda: seen.append(2))
        c = dispatcher.submit("s", lambda: seen.append(3))
        rejected = dispatcher.submit("s", lambda: None)
        self.assertIsInstance(rejected.exception(), RuntimeError)
        gate.set()
        a.result(3)
        b.result(3)
        c.result(3)
        dispatcher.shutdown()
        self.assertEqual(seen, [1, 2, 3])
        self.assertFalse(any(t.is_alive() for t in dispatcher.threads))


class ConfigTests(Fixture):
    def test_deep_merge_validation_hot_reload(self):
        config.save_config(
            {"vlm": {"api_key": "fake"}, "channels": {"napcat": {"owner_ids": ["123"]}}}
        )
        cfg = config.load_config()
        cfg["channels"]["pet"]["scale"] = 9
        self.assertNotEqual(config.load_config()["channels"]["pet"]["scale"], 9)
        with self.assertRaises(ValueError):
            config.save_config({"proactive": {"heartbeat_interval_sec": 0}})
        self.assertEqual(config.load_config()["vlm"]["api_key"], "fake")
        config.save_config({"vlm": {"model": "new"}})
        from core.brain import VLMBrain

        brain = VLMBrain()
        self.assertEqual(brain._build_payload([])["model"], "new")
        config.save_config({"vlm": {"model": "newer"}})
        self.assertEqual(brain._build_payload([])["model"], "newer")


class AdditionalRegressionTests(Fixture):
    def test_archived_evidence_and_session_boundary(self):
        message = self.memory.append_message(
            "a", {"role": "user", "content": "original evidence"}
        )
        self.memory.save_summary("a", message["_id"], "summary")
        self.assertEqual(self.memory.get_history("a")[0], [])
        self.assertEqual(
            self.memory.read_archived_history("a")[0]["content"], "original evidence"
        )
        self.assertEqual(self.memory.read_archived_history("b"), [])

    def test_runtime_reconfigure_and_pending_cancellation(self):
        dispatcher = Dispatcher(1, 8)
        gate = threading.Event()
        start = threading.Event()
        first = dispatcher.submit("a", lambda: (start.set(), gate.wait(3)))
        self.assertTrue(start.wait(1))
        pending = dispatcher.submit("a", lambda: None)
        self.assertEqual(dispatcher.cancel_pending("a"), 1)
        self.assertTrue(pending.cancelled())
        dispatcher.reconfigure(2, 8)
        self.assertEqual(dispatcher.submit("b", lambda: 42).result(1), 42)
        gate.set()
        first.result(2)
        dispatcher.reconfigure(1, 4)
        dispatcher.shutdown()
        self.assertFalse(any(t.is_alive() for t in dispatcher.threads))

    def test_background_does_not_reorder_same_session(self):
        dispatcher = Dispatcher(1, 8)
        gate = threading.Event()
        start = threading.Event()
        order = []
        a = dispatcher.submit("x", lambda: (start.set(), gate.wait(2)))
        self.assertTrue(start.wait(1))
        b = dispatcher.submit("s", lambda: order.append("background"), background=True)
        c = dispatcher.submit("s", lambda: order.append("foreground"))
        gate.set()
        a.result(2)
        b.result(2)
        c.result(2)
        dispatcher.shutdown()
        self.assertEqual(order, ["background", "foreground"])

    def test_subprocess_cancel(self):
        import sys

        event = threading.Event()
        threading.Timer(0.2, event.set).start()
        result = run_bounded(
            [sys.executable, "-c", "import time;time.sleep(10)"], cancel_event=event
        )
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["ok"])

    def test_remote_permission_rechecked_and_conditions_protected(self):
        h = AgentHarness(
            None,
            FakeJev(),
            self.memory,
            FakePerception(),
            SkillManager(self.root / "skills"),
        )
        target = self.root / "private"
        result = h._dispatch_tool(
            "write_file",
            {"file_path": str(target), "content": "x"},
            scope="qq:private:123",
        )
        self.assertFalse(result["ok"])
        self.assertFalse(target.exists())
        result = h._dispatch_tool(
            "schedule_reminder",
            {"trigger_at": 0, "message": "x", "condition": "window:private"},
            scope="qq:private:123",
            permitted={"schedule_reminder"},
        )
        self.assertFalse(result["ok"])

    def test_notification_feedback_changes_policy(self):
        self.memory.feedback("local", "notification", "not_useful")
        self.assertEqual(
            self.memory.notification_preferences("local")["cooldown_multiplier"], 2
        )
        self.memory.feedback("qq:1", "notification", "mute")
        self.assertTrue(self.memory.notification_preferences("qq:1")["muted"])
        self.assertFalse(self.memory.notification_preferences("local")["muted"])

    def test_incomplete_tool_group_recovered_on_restart(self):
        self.memory.append_message("a", {"role": "user", "content": "original"})
        self.memory.append_message(
            "a",
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "unfinished",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
        )

        class Brain:
            def stream_chat_and_tools(self, messages, tools=None):
                seen.extend(messages)
                yield ("text_delta", "recovered")
                return ("recovered", [], {})

        seen = []
        h = AgentHarness(
            Brain(),
            FakeJev(),
            self.memory,
            FakePerception(),
            SkillManager(self.root / "skills"),
        )
        h.run("continue", session_id="a")
        self.assertTrue(any(m.get("tool_call_id") == "unfinished" for m in seen))

    def test_python_skill_worker_unicode_and_timeout_isolation(self):
        folder = self.root / "skills"
        folder.mkdir()
        (folder / "system_status.py").write_text(
            "def execute(context): return '中文输出'", encoding="utf-8"
        )
        manager = SkillManager(folder)
        result = manager.execute_skill("system_status")
        self.assertTrue(result["ok"])
        self.assertIn("中文输出", result["output"])


class EndToEndTests(Fixture):
    def test_application_offline_reminder_and_owner_boundary(self):
        import contextlib
        import io
        import main

        with (
            patch.object(main, "MiniMemory", return_value=self.memory),
            patch.object(main, "LocalPerception", FakePerception),
            patch.object(
                main, "SkillManager", return_value=SkillManager(self.root / "skills")
            ),
        ):
            app = main.MiniBotApp("local")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                reply = app.submit_query("20分钟后提醒我喝水").result(3)
            self.assertIn("已设置", reply)
            task = self.memory.list_intents()[0]
            self.assertAlmostEqual(task["trigger_at"] - time.time(), 1200, delta=3)
            event = {"message_type": "private", "user_id": 123}
            scope, session, channel, target, tools = app.identity(event)
            self.assertFalse(tools)
            self.assertEqual(channel, "qq_private")
            with contextlib.redirect_stdout(io.StringIO()):
                app.submit_query("1秒后提醒我回复邮件", event).result(3)
            self.assertEqual(len(self.memory.list_intents(scope, session)), 1)
            self.assertEqual(len(self.memory.list_intents()), 1)
            cancel = app.submit_query("/cancel").result(1)
            self.assertIn("已取消", cancel)
        finally:
            app.stop()
        self.assertFalse(any(t.is_alive() for t in app.dispatcher.threads))

    def test_secrets_live_in_single_config_file(self):
        config.save_config(
            {
                "vlm": {"api_key": "PRIVATE_TEST"},
                "channels": {"napcat": {"access_token": "TOKEN_TEST"}},
            }
        )
        stored = (self.root / "config.json").read_text(encoding="utf-8")
        self.assertIn("PRIVATE_TEST", stored)
        self.assertIn("TOKEN_TEST", stored)
        self.assertFalse((self.root / "config.local.json").exists())
        config._cache = None
        config._stamp = None
        self.assertEqual(config.load_config()["vlm"]["api_key"], "PRIVATE_TEST")

    def test_legacy_local_config_is_merged_once(self):
        (self.root / "config.json").write_text(
            json.dumps({"vlm": {"model": "m1"}}, ensure_ascii=False), encoding="utf-8"
        )
        (self.root / "config.local.json").write_text(
            json.dumps(
                {
                    "vlm": {"api_key": "K1", "model_keys": {"m1": "K1"}},
                    "channels": {"napcat": {"access_token": "T1"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config._cache = None
        config._stamp = None
        cfg = config.load_config()
        self.assertEqual(cfg["vlm"]["api_key"], "K1")
        self.assertEqual(cfg["vlm"]["model"], "m1")
        self.assertEqual(cfg["channels"]["napcat"]["access_token"], "T1")
        self.assertFalse((self.root / "config.local.json").exists())
        self.assertTrue((self.root / "config.local.json.bak").exists())
        merged = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["vlm"]["api_key"], "K1")
        self.assertNotIn("model_keys", merged["vlm"])

    def test_relative_and_calendar_parser(self):
        from core.reminders import parse_reminder

        now = time.time()
        due, message = parse_reminder("1.5小时后提醒我吃饭", now)
        self.assertEqual(due, now + 5400)
        self.assertEqual(message, "吃饭")
        self.assertIsNotNone(parse_reminder("明天9点30提醒我开会", now))
        self.assertIsNone(parse_reminder("我明天可能开会", now))
        with self.assertRaises(ValueError):
            parse_reminder("明天25点提醒我开会", now)


class BudgetTests(Fixture):
    def test_context_overflow_is_reported_without_invalid_request(self):
        config.save_config({"vlm": {"context_window": 2048, "max_tokens": 128}})

        class Brain:
            def stream_chat_and_tools(self, *a, **k):
                raise AssertionError("should not call an oversized request")

        h = AgentHarness(
            Brain(),
            FakeJev(),
            self.memory,
            FakePerception(),
            SkillManager(self.root / "skills"),
        )
        self.assertIn("上下文超过", h.run("x" * 5000))
        self.assertIn("x" * 5000, json.dumps(self.memory.get_history("local:pet")))

    def test_old_tool_output_trimming_preserves_status(self):
        messages = [
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "ok": False,
                        "exit_code": 1,
                        "error": "failed",
                        "output": "x" * 5000,
                    }
                ),
            },
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "next"},
        ]
        messages += [{"role": "assistant", "content": "recent"} for _ in range(20)]
        compact = ContextCompactor.trim_old_outputs(messages)
        payload = json.loads(compact[0]["content"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertLess(len(compact[0]["content"]), 1300)

    def test_native_powershell_unicode(self):
        import os

        if os.name != "nt":
            self.skipTest("Windows only")
        h = AgentHarness(
            None,
            FakeJev(),
            self.memory,
            FakePerception(),
            SkillManager(self.root / "skills"),
        )
        result = h._dispatch_tool(
            "execute_command", {"command": "Write-Output '中文输出'"}
        )
        self.assertTrue(result["ok"])
        self.assertIn("中文输出", result["output"])


class EnvironmentTests(Fixture):
    def test_watched_file_drives_collaboration(self):
        path = self.root / "build.log"
        path.write_text("start")
        config.save_config(
            {"proactive": {"watch_paths": [str(path)], "quiet_hours": [0, 0]}}
        )
        cfg = config.load_config()["proactive"]
        sent = []
        calls = []

        class Jev:
            def is_configured(self):
                return True

            def decide(self, *args, **kwargs):
                calls.append(args)
                return {"intervene": 1, "posture": "task_progress_update"}

        class Brain:
            def generate_bubble_reply(self, *args, **kwargs):
                return "监控文件有更新"

        engine = ProactiveEngine(
            self.memory,
            FakePerception(),
            Jev(),
            Brain(),
            None,
            lambda *a, **k: sent.append(a) or True,
        )
        engine._tick(cfg)
        self.assertFalse(calls)
        path.write_text("completed build")
        engine._tick(cfg)
        self.assertEqual(len(sent), 1)
        self.assertTrue(engine._last_state["environment_events"])

    def test_muting_defers_followup_but_not_explicit_reminders(self):
        self.memory.add_delayed_intent(0, "automatic", kind="followup")
        self.memory.add_delayed_intent(0, "explicit")
        cfg = config.load_config()["proactive"]
        cfg["muted"] = True
        sent = []
        engine = ProactiveEngine(
            self.memory,
            FakePerception(),
            FakeJev(),
            None,
            None,
            lambda *a, **k: sent.append(a[0]) or True,
        )
        engine._tick(cfg)
        self.assertEqual(sent, ["explicit"])
        self.assertEqual(
            {t["kind"]: t["status"] for t in self.memory.list_intents()},
            {"reminder": "delivered", "followup": "pending"},
        )

    def test_final_recheck_suppresses_late_notification(self):
        config.save_config({"proactive": {"quiet_hours": [0, 0]}})
        cfg = config.load_config()["proactive"]
        perception = FakePerception()
        sent = []

        class Jev:
            def decide(self, *a, **k):
                return {"intervene": 1, "posture": "debug_assist"}

        class Brain:
            def generate_bubble_reply(self, *a, **k):
                perception.state["idle_sec"] = 0
                return "message generated too late"

        engine = ProactiveEngine(
            self.memory,
            perception,
            Jev(),
            Brain(),
            None,
            lambda *a, **k: sent.append(a) or True,
        )
        engine._decide(dict(perception.state, has_error=True), cfg)
        self.assertEqual(sent, [])

    def test_windows_child_process_tree_terminated(self):
        import os
        import sys
        import ctypes

        if os.name != "nt":
            self.skipTest("Windows Job Object test")
        code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
        result = run_bounded([sys.executable, "-c", code], timeout=0.7)
        self.assertTrue(result["timeout"])
        pid = int(result["output"].strip().splitlines()[0])
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        from ctypes import wintypes as w

        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
            finally:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
