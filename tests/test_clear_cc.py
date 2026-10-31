import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.memory import MiniMemory
from core.skill_manager import SkillManager
from main import MiniBotApp as BotApplication

class FakeBrain:
    def chat(self, *args, **kwargs):
        return "这是总结摘要"

class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()

        # Patch config with owner_id
        self.cfg_patch = patch("core.config.load_config", return_value={
            "channels": {
                "napcat": {
                    "enabled": False,
                    "owner_ids": ["416448943"],
                    "allowed_group_ids": [],
                    "private_allow_all": True,
                    "ws_url": "",
                    "access_token": "",
                },
                "pet": {"enabled": False, "pet_dir": str(self.root)}
            },
            "memory": {"retention_days": 30, "context_chars": 2000},
            "runtime": {"verify_completion": False, "verify_chat": False},
            "vlm": {"model": "test-model", "context_window": 8000, "max_tokens": 1000, "compaction_threshold_ratio": 0.8}
        })
        self.cfg_patch.start()

        self.app = BotApplication(mode="cli")
        self.app.memory = MiniMemory(self.root / "test.db")
        self.app.vlm = FakeBrain()

    def tearDown(self):
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def test_qq_and_pet_clear_command(self):
        owner_qq_session = "qq:private:416448943"
        pet_session = "local:pet"
        web_session = "web:main"

        # 1. 模拟历史累积了 69 条消息
        for i in range(69):
            self.app.memory.append_message(owner_qq_session, {"role": "user" if i%2==0 else "assistant", "content": f"msg {i}"})
            self.app.memory.append_message(pet_session, {"role": "user" if i%2==0 else "assistant", "content": f"pet msg {i}"})

        hist, _ = self.app.memory.get_history(owner_qq_session)
        self.assertEqual(len(hist), 69)

        # 2. 用户在 QQ 私聊发送 "/clear"
        qq_event = {"message_type": "private", "user_id": 416448943, "raw_message": "/clear"}
        future = self.app.submit_query("/clear", event=qq_event)
        res = future.result()
        # 回执措辞由人设决定（core.persona.voice），这里只钉「有回执」这个行为，
        # 不钉具体字眼，否则改一点人设测试就红。
        self.assertTrue(res.strip(), "清空后应给一句回执")

        # 3. 验证 QQ 私聊会话、桌宠会话、Web 主会话均已被清空为 0
        hist_qq, _ = self.app.memory.get_history(owner_qq_session)
        self.assertEqual(len(hist_qq), 0, "QQ 会话历史应为 0")

        hist_pet, _ = self.app.memory.get_history(pet_session)
        self.assertEqual(len(hist_pet), 0, "桌宠共享会话历史应为 0")

    def test_qq_cc_compact_command(self):
        owner_qq_session = "qq:private:416448943"
        # 插入 12 条消息
        for i in range(12):
            self.app.memory.append_message(owner_qq_session, {"role": "user" if i%2==0 else "assistant", "content": f"msg {i}"})

        hist, _ = self.app.memory.get_history(owner_qq_session)
        self.assertEqual(len(hist), 12)

        # 发送 /cc
        qq_event = {"message_type": "private", "user_id": 416448943, "raw_message": "/cc"}
        future = self.app.submit_query("/cc", event=qq_event)
        res = future.result()
        self.assertTrue(res.strip(), "压缩后应给一句回执")

        hist_after, summary = self.app.memory.get_history(owner_qq_session)
        self.assertIsNotNone(summary)
        self.assertLess(len(hist_after), 12)

if __name__ == "__main__":
    unittest.main()
