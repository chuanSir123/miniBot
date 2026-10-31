"""私聊表情包收集 + 聊天内重启能力。

两件事：
1. 表情包静默采集从"只有群聊"扩到"群聊 + 私聊"，来源标签分开记。
2. 聊天里能调用重启：主人有 restart_bot 工具与 /重启 指令，
   群友一律按权限拒绝；进程以约定退出码结束，由启动器（或自己）重新拉起。
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import launcher
from test_core import Fixture, FakeJev, FakePerception
from core import config
from core.context_builder import TOOL_SNIPPETS
from core.harness import AgentHarness, HARNESS_TOOLS
from core.qq_policy import QQPolicy
from core.skill_manager import SkillManager
from channels.napcat_channel import NapCatChannel


class MemeCollectScopeTests(Fixture):
    """私聊发的图也要静默进表情包库。"""

    def setUp(self):
        super().setUp()
        config.save_config({
            "channels": {"napcat": {"owner_ids": ["123"], "private_allow_all": True,
                                    "allowed_group_ids": ["88"], "group_members_all": True}}
        })

    def _parsed(self, message_id):
        return {
            "text": "看这个表情", "raw_text": "看这个表情", "direct_text": "看这个表情",
            "images": [],
            "media": [{"media_ref": "media://img_png", "message_id": str(message_id),
                       "origin": "received", "mime": "image/png"}],
            "errors": [], "mentioned": False, "reply_to_bot": False, "quoted_messages": [],
        }

    async def _collect(self, event, message_id):
        policy = MagicMock()
        policy.blocked.return_value = False
        policy.owner_command.return_value = None
        policy.decide_reply.return_value = False
        channel = NapCatChannel(on_message_callback=MagicMock(), policy=policy)
        with patch("channels.qq_media.MessageParser") as parser:
            parser.return_value.parse = AsyncMock(return_value=self._parsed(message_id))
            with patch.object(channel, "_try_collect_meme", new_callable=AsyncMock) as collect:
                await channel._handle_onebot_event(event)
                await asyncio.sleep(0.05)   # 让 create_task 起来的收集任务跑完
        return collect

    def test_private_chat_image_is_collected_as_private_stealer(self):
        event = {"message_type": "private", "user_id": 123, "self_id": 999, "message_id": 77,
                 "post_type": "message", "sender": {"nickname": "主人"},
                 "message": [{"type": "text", "data": {"text": "看这个表情"}}]}
        collect = asyncio.run(self._collect(event, 77))
        collect.assert_called_once()
        asset, scope, source = collect.call_args.args
        self.assertEqual(asset["media_ref"], "media://img_png")
        # 私聊的媒体归属键是 qq:private:<QQ>，取回字节时必须用同一个键
        self.assertEqual(scope, "qq:private:123")
        self.assertEqual(source, "private_stealer")

    def test_group_chat_image_stays_group_stealer(self):
        event = {"message_type": "group", "group_id": 88, "user_id": 123, "self_id": 999,
                 "message_id": 88, "post_type": "message", "sender": {"nickname": "主人"},
                 "message": [{"type": "text", "data": {"text": "看这个表情"}}]}
        collect = asyncio.run(self._collect(event, 88))
        collect.assert_called_once()
        _, scope, source = collect.call_args.args
        self.assertEqual(scope, "qq:group:88")
        self.assertEqual(source, "group_stealer")

    def test_private_collect_can_be_switched_off(self):
        config.save_config({"meme": {"collect_private": False}})
        event = {"message_type": "private", "user_id": 123, "self_id": 999, "message_id": 78,
                 "post_type": "message", "sender": {"nickname": "主人"},
                 "message": [{"type": "text", "data": {"text": "看这个表情"}}]}
        collect = asyncio.run(self._collect(event, 78))
        collect.assert_not_called()

    def test_try_collect_meme_reads_bytes_from_the_same_scope(self):
        """收集时必须按同一 scope 取回原图字节，动图才不会被压成首帧。"""
        channel = NapCatChannel()
        added = []

        class FakeManager:
            def add_from_bytes(self, data, source):
                added.append((data, source))

        with patch("core.media_store.MediaStore") as store_cls, \
                patch("channels.napcat_channel.get_meme_manager", return_value=FakeManager()):
            store_cls.return_value.get.return_value = ({}, b"raw-bytes")
            asyncio.run(channel._try_collect_meme(
                {"media_ref": "media://img_png"}, "qq:private:123", "private_stealer"))
        self.assertEqual(store_cls.return_value.get.call_args.args, ("media://img_png", "qq:private:123"))
        self.assertEqual(added, [(b"raw-bytes", "private_stealer")])


class RestartToolTests(Fixture):
    """聊天内重启：主人能叫，群友不能。"""

    def setUp(self):
        super().setUp()
        config.save_config({"channels": {"napcat": {"owner_ids": ["123"], "allow_owner_tools": True}}})
        self.names = {t["function"]["name"] for t in HARNESS_TOOLS}

    def harness(self):
        return AgentHarness(MagicMock(), FakeJev(), self.memory, FakePerception(),
                             SkillManager(self.root / "skills"))

    def test_tool_is_registered_and_documented(self):
        self.assertIn("restart_bot", self.names)
        self.assertIn("restart_bot", TOOL_SNIPPETS)

    def test_owner_can_restart_and_member_is_denied(self):
        harness = self.harness()
        calls = []
        harness.on_restart = lambda reason, session="": calls.append((reason, session)) or True

        ok = harness._dispatch_tool("restart_bot", {"reason": "改了配置要生效"},
                                    scope="qq:private:123", session_id="qq:private:123",
                                    channel="qq_private", actor="qq:private:123")
        self.assertTrue(ok["ok"])
        self.assertTrue(ok["restarting"])
        self.assertEqual(calls, [("改了配置要生效", "qq:private:123")])

        denied = harness._dispatch_tool("restart_bot", {"reason": "我也想重启"},
                                        scope="qq:group:88", session_id="qq:group:88",
                                        channel="qq_group", actor="qq:group:88:user:456")
        self.assertEqual(denied["error_code"], "PERMISSION_DENIED")
        self.assertEqual(len(calls), 1)

    def test_owner_may_grant_restart_to_someone_in_the_group(self):
        """重启不是"只有主人能碰"：主人可以显式授权，授权后按授权表放行。"""
        from core.qq_authority import manage_permissions, tool_access

        manage_permissions("qq:group:88:user:123",
                           {"action": "grant", "user_id": "456", "tools": ["restart_bot"]},
                           self.names)
        self.assertTrue(tool_access("qq:group:88", "restart_bot", actor="qq:group:88:user:456"))
        self.assertFalse(tool_access("qq:group:88", "restart_bot", actor="qq:group:88:user:789"))

    def test_missing_host_callback_is_reported_not_faked(self):
        harness = self.harness()
        harness.on_restart = None
        result = harness._dispatch_tool("restart_bot", {"reason": "x"},
                                        scope="qq:private:123", session_id="qq:private:123",
                                        channel="qq_private", actor="qq:private:123")
        self.assertFalse(result["ok"])
        self.assertIn("重启", result["error"])


class RestartCommandTests(Fixture):
    """主人直接打 /重启，不必等模型决定要不要调工具。"""

    def setUp(self):
        super().setUp()
        config.save_config({"channels": {"napcat": {"owner_ids": ["123"]}}})

    def test_owner_command_triggers_restart_and_others_do_not(self):
        policy = QQPolicy(self.memory, FakeJev())
        calls = []
        policy.on_restart = lambda reason: calls.append(reason) or True

        reply = policy.owner_command("/重启", {"user_id": 123, "message_type": "private"})
        self.assertIn("重启", reply)
        self.assertEqual(len(calls), 1)

        # 群友打同样的指令：不执行，也不冒充已重启
        self.assertIsNone(policy.owner_command("/重启",
                                               {"user_id": 456, "message_type": "group", "group_id": 88}))
        self.assertEqual(len(calls), 1)

    def test_command_without_host_callback_says_so(self):
        policy = QQPolicy(self.memory, FakeJev())
        reply = policy.owner_command("/restart", {"user_id": 123, "message_type": "private"})
        self.assertIn("重启", reply)


class LauncherRestartTests(unittest.TestCase):
    """启动器当看护：main 以退出码 42 结束就重新拉起。"""

    def test_restart_exit_code_relaunches_the_child(self):
        with patch.object(launcher.subprocess, "call",
                          side_effect=[launcher.RESTART_EXIT_CODE, 0]) as call, \
                patch.object(launcher.time, "sleep"):
            self.assertEqual(launcher._run_main("napcat"), 0)
        self.assertEqual(call.call_count, 2)
        # 子进程必须带上"外面有人看着"的标记，main 才不会自己再拉一个
        for entry in call.call_args_list:
            self.assertEqual(entry.kwargs["env"][launcher.SUPERVISED_ENV], "1")
            self.assertEqual(entry.kwargs["cwd"], launcher.PROJECT_ROOT)

    def test_normal_exit_is_not_restarted(self):
        with patch.object(launcher.subprocess, "call", return_value=0) as call, \
                patch.object(launcher.time, "sleep"):
            self.assertEqual(launcher._run_main("napcat"), 0)
        self.assertEqual(call.call_count, 1)

    def test_crash_loop_stops_instead_of_respawning_forever(self):
        with patch.object(launcher.subprocess, "call",
                          return_value=launcher.RESTART_EXIT_CODE) as call, \
                patch.object(launcher.time, "sleep"), \
                patch.object(launcher, "FAST_RESTART_SEC", 10 ** 9):
            code = launcher._run_main("napcat")
        self.assertEqual(code, launcher.RESTART_EXIT_CODE)
        self.assertEqual(call.call_count, launcher.MAX_FAST_RESTARTS + 1)


class RestartExitCodeTests(unittest.TestCase):
    """main.py 的退出码与自拉起约定。"""

    def test_constants_agree_between_main_and_launcher(self):
        import main

        self.assertEqual(main.RESTART_EXIT_CODE, launcher.RESTART_EXIT_CODE)
        self.assertEqual(main.SUPERVISED_ENV, launcher.SUPERVISED_ENV)

    def test_parent_pid_is_used_to_wait_for_the_previous_instance(self):
        import main

        self.assertTrue(hasattr(main, "_wait_for_parent_exit"))
        self.assertTrue(hasattr(main, "_spawn_self_restart"))


class AppRestartFlowTests(Fixture):
    """宿主侧的停机时序：回复发完 -> 优雅停机 -> 交出退出码。"""

    def _app(self):
        import threading

        from main import MiniBotApp

        app = MiniBotApp.__new__(MiniBotApp)   # 绕开重依赖的构造函数
        app.mode = "napcat"
        app._restart_requested = False
        app._restart_reason = ""
        app._restart_lock = threading.Lock()
        app._stop = threading.Event()
        app._qt_app = None
        app.napcat_channel = None

        class FakeHarness:
            def is_session_active(self, session):
                return False

        app.harness = FakeHarness()
        return app

    def test_request_restart_stops_the_app_once_the_turn_is_over(self):
        import time

        app = self._app()
        self.assertTrue(app.request_restart("改了配置", "qq:private:123"))
        # 重复请求不重复受理（否则会起两个停机线程）
        self.assertFalse(app.request_restart("再来一次", "qq:private:123"))
        deadline = time.time() + 8
        while time.time() < deadline and not app._stop.is_set():
            time.sleep(0.05)
        self.assertTrue(app._stop.is_set())
        self.assertTrue(app.restart_requested)
        self.assertEqual(app._restart_reason, "改了配置")


if __name__ == "__main__":
    unittest.main()
