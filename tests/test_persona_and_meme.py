import unittest
import asyncio
from unittest.mock import AsyncMock, patch
from pathlib import Path
from test_core import Fixture

from core import config
from core.persona import persona_prompt, names
from core.meme import get_meme_manager, EMOTION_KEYWORDS, extract_meme_tag
from core.qq_policy import QQPolicy
from channels.napcat_channel import NapCatChannel


class PersonaAndMemeTests(Fixture):
    def test_error_reply_is_one_message_and_paragraphs_do_not_lose_text(self):
        channel = NapCatChannel()
        error = "【模型接口错误：HTTP 520。请检查服务端；请求编号 test。】"
        event = {"user_id": 1, "message_type": "private"}
        for text, expected in ((error, [error]), ("你好。很高兴见到你！", ["你好。", "很高兴见到你！"]),
                               ("一\n\n二\n\n三\n\n四", ["一\n\n二\n\n三\n\n四"]),
                               ("第一句。（这是说明。）", ["第一句。", "（这是说明。）"]),
                               ("第一句。喵。第二句！", ["第一句。喵。", "第二句！"]),
                               ("好。收到啦！", ["好。收到啦！"]),
                               ("完成。（提示。）", ["完成。", "（提示。）"])):
            with patch.object(channel, "_send", new_callable=AsyncMock, return_value=True) as send, patch("channels.napcat_channel.asyncio.sleep", new_callable=AsyncMock), patch("channels.napcat_channel.get_meme_manager") as memes:
                memes.return_value.choose_meme.return_value = None
                asyncio.run(channel.send_reply_segments(event, text))
            self.assertEqual([c.args[2] for c in send.call_args_list], expected)

    def test_persona_and_owner_prompt(self):
        config.save_config({
            "persona": {
                "name": "喵喵",
                "aliases": ["喵喵", "猫娘", "小猫"],
                "prompt": "可爱猫娘"
            },
            "channels": {
                "napcat": {
                    "owner_ids": ["123456", "789012"]
                }
            }
        })
        prompt = persona_prompt()
        self.assertIn("喵喵", prompt)
        self.assertIn("123456", prompt)
        self.assertIn("789012", prompt)
        all_names = names()
        self.assertIn("喵喵", all_names)
        self.assertIn("猫娘", all_names)

    def test_meme_extraction_and_keyword_matching(self):
        text, hint = extract_meme_tag("主人今天辛苦啦喵~ [表情: 摸摸]")
        self.assertEqual(text, "主人今天辛苦啦喵~")
        self.assertEqual(hint, "摸摸")

        clean, no_hint = extract_meme_tag("普通的对话内容")
        self.assertEqual(clean, "普通的对话内容")
        self.assertIsNone(no_hint)

    def test_owner_block_persists_to_config(self):
        policy = QQPolicy(self.memory, None)
        reply = policy.owner_command("/拉黑 999888", event={"user_id": 1})
        self.assertIsNone(reply)  # 1 不是 owner

        config.save_config({"channels": {"napcat": {"owner_ids": ["1"]}}})
        reply = policy.owner_command("/拉黑 999888", event={"user_id": 1})
        self.assertIn("已将 999888 加入黑名单", reply)
        self.assertTrue(self.memory.qq_blocked("999888"))
        cfg = config.load_config()
        self.assertIn("999888", [str(x) for x in cfg["channels"]["napcat"]["blacklisted_user_ids"]])

        # 取消拉黑
        reply_unblock = policy.owner_command("/取消拉黑 999888", event={"user_id": 1})
        self.assertIn("已解除", reply_unblock)
        self.assertFalse(self.memory.qq_blocked("999888"))
        cfg = config.load_config()
        self.assertNotIn("999888", [str(x) for x in cfg["channels"]["napcat"]["blacklisted_user_ids"]])


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!" + b"\x00" * 12
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32


class MemeLibraryTests(Fixture):
    def _manager(self):
        from core.meme import MemeManager

        return MemeManager(meme_dir=self.root / "memes", db_path=self.root / "memes.db")

    def _add(self, manager, data, tags):
        import hashlib
        import json

        path = manager.add_from_bytes(data, "test")
        with manager._db() as db:
            db.execute(
                "UPDATE memes SET tags = ?, tag_source = 'vlm', tagged_at = ? WHERE file_hash = ?",
                (json.dumps(tags, ensure_ascii=False), 1.0, hashlib.md5(data).hexdigest()),
            )
        manager.rebuild_tag_summary()
        return path

    def test_collect_keeps_container_ext_and_use_count(self):
        from core.meme import MemeManager

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self.assertTrue(manager.add_from_bytes(GIF_BYTES).endswith(".gif"))
            self.assertTrue(manager.add_from_bytes(JPEG_BYTES).endswith(".jpg"))
            self.assertTrue(manager.add_from_bytes(PNG_BYTES).endswith(".png"))

            # 同一张图再收一次：不能把使用计数清零（旧实现用 INSERT OR REPLACE 会）
            with manager._db() as db:
                db.execute("UPDATE memes SET use_count = 5 WHERE file_hash IS NOT NULL")
            manager.add_from_bytes(GIF_BYTES)
            with manager._db() as db:
                counts = [row["use_count"] for row in db.execute("SELECT use_count FROM memes")]
            self.assertIn(5, counts)

    def test_tag_summary_and_pick_by_tag(self):
        from core.meme import MemeManager

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["卖萌", "猫"])
            self._add(manager, GIF_BYTES, ["大笑", "开心"])

            summary = {item["tag"]: item["count"] for item in manager.tag_summary()}
            self.assertEqual(summary.get("卖萌"), 1)
            self.assertEqual(summary.get("大笑"), 1)

            picked = manager.pick_by_tag("卖萌")
            self.assertTrue(picked and picked.endswith(".png"))
            self.assertIsNone(manager.pick_by_tag("不存在的标签"))
            # 挑过一次就计数加一，用于让图库轮转
            again = [item for item in manager.tag_summary() if item["tag"] == "卖萌"][0]
            self.assertEqual(again["used"], 1)

    def test_choose_meme_respects_jev_verdict(self):
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["卖萌", "猫"])

            with patch.object(JevClient, "select_meme_tag", return_value=None):
                self.assertIsNone(manager.choose_meme("在吗", "喵～在的"))
            with patch.object(JevClient, "select_meme_tag", return_value="卖萌"):
                self.assertTrue(manager.choose_meme("在吗", "喵～在的"))

    def test_session_cooldown_rotates_and_rests(self):
        """同一会话内不重发：两张图轮换，全冷却后这轮歇着。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            path_a = self._add(manager, PNG_BYTES, ["卖萌"])
            path_b = self._add(manager, JPEG_BYTES, ["卖萌"])

            picks = []
            for _ in range(3):
                with patch.object(JevClient, "select_meme_tag", return_value="卖萌"):
                    picked = manager.choose_meme("在吗", "喵～在的", chat_key="group:1")
                if picked:
                    manager.note_sent("group:1", picked)
                picks.append(picked)

            self.assertIsNotNone(picks[0])
            self.assertIsNotNone(picks[1])
            self.assertNotEqual(picks[0], picks[1])       # 不重发同一张
            self.assertEqual({picks[0], picks[1]}, {path_a, path_b})
            self.assertIsNone(picks[2])                    # 两张都冷却 -> 这轮不发

    def test_cooldown_is_per_conversation(self):
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            path_a = self._add(manager, PNG_BYTES, ["卖萌"])
            path_b = self._add(manager, JPEG_BYTES, ["卖萌"])

            with patch.object(JevClient, "select_meme_tag", return_value="卖萌"):
                first = manager.choose_meme("在吗", "喵～在的", chat_key="group:1")
                manager.note_sent("group:1", first)
                again_other_chat = manager.choose_meme("在吗", "喵～在的", chat_key="group:2")
            self.assertIn(again_other_chat, {path_a, path_b})   # 别的会话不受冷却影响

    def test_cooled_tag_never_reaches_jev(self):
        """标签下唯一一张图在冷却 -> 这个标签整个不进 Jev 候选。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            liked = self._add(manager, PNG_BYTES, ["赞同"])
            laugh = self._add(manager, JPEG_BYTES, ["大笑"])
            manager.note_sent("group:1", liked)

            with patch.object(JevClient, "select_meme_tag", return_value="大笑") as sel:
                manager.choose_meme("在吗", "喵～在的", chat_key="group:1")
            offered = sel.call_args.kwargs["tags"]
            self.assertNotIn("赞同", offered)     # 只有一张且在冷却，选了也发不出
            self.assertIn("大笑", offered)

    def test_avoid_repeat_last_limits_memory(self):
        from core.meme import MemeManager

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            # 尾部附加不同字节让 md5 各不相同，得到 12 个独立文件
            paths = [self._add(manager, PNG_BYTES + bytes([i]), [f"标签{i}"]) for i in range(12)]
            self.assertEqual(len(set(paths)), 12)
            for p in paths:
                manager.note_sent("group:1", p)
            cooled = manager._cooldown_paths("group:1")
            self.assertLessEqual(len(cooled), 8)          # 默认 avoid_repeat_last=8
            self.assertIn(paths[-1], cooled)              # 最近发的还在
            self.assertNotIn(paths[0], cooled)            # 最早的已被挤出去


    def test_meme_sent_at_most_once_per_turn(self):
        channel = NapCatChannel()
        event = {"user_id": 1, "message_type": "private"}
        with patch("channels.napcat_channel.get_meme_manager") as memes:
            memes.return_value.choose_meme.return_value = __file__
            first = asyncio.run(channel._pick_meme_for_turn(event, "好呀"))
            second = asyncio.run(channel._pick_meme_for_turn(event, "好呀"))
        self.assertEqual(first, __file__)
        self.assertIsNone(second)
        self.assertEqual(memes.return_value.choose_meme.call_count, 1)

    def test_meme_decision_stops_after_two_tries(self):
        channel = NapCatChannel()
        event = {"user_id": 1, "message_type": "private"}
        with patch("channels.napcat_channel.get_meme_manager") as memes:
            memes.return_value.choose_meme.return_value = None
            for _ in range(3):
                asyncio.run(channel._pick_meme_for_turn(event, "好呀"))
        # 进度一次 + 终稿一次，之后不再问 Jev
        self.assertEqual(memes.return_value.choose_meme.call_count, 2)


class SelectMemeTagTests(Fixture):
    """Jev 选标签的分批穷尽：候选问完必然终止，不无限循环，也不硬凑。"""

    def _client(self):
        from core.jev import JevClient

        # 给一个假 key，只为让 is_configured() 通过；decide_raw 全被替换掉。
        return JevClient(base_url="http://127.0.0.1:1", api_key="test-key")

    @staticmethod
    def _answer(choice, confidence):
        return {"answers": {"meme_tag": {"type": "choice", "choice": choice,
                                         "probabilities": {}, "confidence": confidence}}}

    def test_exhausts_every_candidate_before_giving_up(self):
        client = self._client()
        calls = []

        def fake_decide(state, questions, images=None):
            calls.append(len(questions["meme_tag"]["options"]))
            return self._answer("none", 0.99)

        client.decide_raw = fake_decide
        tags = [f"tag{i}" for i in range(600)]
        # 全部候选都不合适 -> 返回 None，且确实每批都问过（不含无限循环）
        self.assertIsNone(client.select_meme_tag("在吗", "在的", tags))
        # 每批 254 个标签 + none；600 = 254 + 254 + 92
        self.assertEqual(calls, [255, 255, 93])

    def test_stops_as_soon_as_a_confident_tag_is_found(self):
        client = self._client()
        calls = []

        def fake_decide(state, questions, images=None):
            calls.append(len(questions["meme_tag"]["options"]))
            return self._answer("摸摸", 0.91)

        client.decide_raw = fake_decide
        tags = ["摸摸"] + [f"tag{i}" for i in range(599)]
        # 第一批就选准了，不该再去问后面几批
        self.assertEqual(client.select_meme_tag("好累", "早点休息", tags), "摸摸")
        self.assertEqual(len(calls), 1)

    def test_low_confidence_keeps_looking(self):
        client = self._client()
        tags = [f"tag{i}" for i in range(300)]
        calls = []

        def fake_decide(state, questions, images=None):
            calls.append(len(questions["meme_tag"]["options"]))
            if len(calls) == 1:
                # 第一批选中了但没把握 -> 继续问，不拿它硬凑
                return self._answer("tag1", 0.10)
            return self._answer("tag260", 0.88)

        client.decide_raw = fake_decide
        self.assertEqual(client.select_meme_tag("在吗", "在的", tags), "tag260")
        self.assertEqual(len(calls), 2)

    def test_low_confidence_everywhere_means_no_meme(self):
        client = self._client()

        def fake_decide(state, questions, images=None):
            return self._answer("tag0", 0.10)

        client.decide_raw = fake_decide
        tags = [f"tag{i}" for i in range(300)]
        self.assertIsNone(client.select_meme_tag("在吗", "在的", tags))

    def test_medium_confidence_is_accepted_after_exhausting_all_batches(self):
        """0.5 不到「决定性」但高于地板：问完所有批后仍然要发这一次。"""
        client = self._client()
        calls = []

        def fake_decide(state, questions, images=None):
            opts = questions["meme_tag"]["options"]
            calls.append(len(opts))
            return self._answer(opts[0], 0.5)   # 每批都只是"这批里最好的"

        client.decide_raw = fake_decide
        tags = [f"tag{i}" for i in range(300)]
        # 不提前终止（0.5 < decisive 0.6），但也不该被丢掉（0.5 > 地板 0.35）
        self.assertEqual(client.select_meme_tag("在吗", "在的", tags), "tag0")
        self.assertEqual(len(calls), 2)

    def test_keeps_the_best_score_not_the_last_one(self):
        """跨批只按分数取最好的一次，不是拿最后一批的答案。"""
        client = self._client()
        scores = iter([0.55, 0.40])

        def fake_decide(state, questions, images=None):
            opts = questions["meme_tag"]["options"]
            return self._answer(opts[0], next(scores))

        client.decide_raw = fake_decide
        tags = [f"tag{i}" for i in range(300)]
        self.assertEqual(client.select_meme_tag("在吗", "在的", tags), "tag0")

    def test_shrinks_batch_when_server_rejects_option_count(self):
        client = self._client()
        sizes = []

        def fake_decide(state, questions, images=None):
            size = len(questions["meme_tag"]["options"])
            sizes.append(size)
            if size > 10:
                raise RuntimeError("400 too_many_options: options over limit")
            return self._answer("摸摸", 0.9)

        client.decide_raw = fake_decide
        tags = ["摸摸"] + [f"tag{i}" for i in range(100)]
        self.assertEqual(client.select_meme_tag("好累", "早点休息", tags), "摸摸")
        # 首轮 254 个标签的批被拒 -> 砍到 127，但候选只剩 101 个标签，于是仍是 101
        # -> 63 -> 31 -> 15 -> 7 个标签（+none）。全程不跳过任何候选。
        self.assertEqual(sizes, [102, 102, 64, 32, 16, 8])

    def test_stops_on_unexpected_errors(self):
        client = self._client()
        calls = []

        def fake_decide(state, questions, images=None):
            calls.append(1)
            raise TimeoutError("network down")

        client.decide_raw = fake_decide
        tags = [f"tag{i}" for i in range(600)]
        # 网络挂了就收手，不去撞后面几批（否则表情包发送被拖慢好几秒）
        self.assertIsNone(client.select_meme_tag("在吗", "在的", tags))
        self.assertEqual(len(calls), 1)

    def test_returns_none_without_configured_key(self):
        from core.jev import JevClient

        client = JevClient(base_url="http://127.0.0.1:1", api_key="")
        client.decide_raw = lambda *a, **k: self.fail("没有 key 就不该发请求")
        self.assertIsNone(client.select_meme_tag("在吗", "在的", ["卖萌"]))


if __name__ == "__main__":
    unittest.main()
