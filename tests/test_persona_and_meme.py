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
        # 回执由人设生成（没有 brain 时走兜底），这里只要求说到了那个号，
        # 具体措辞不钉死——否则换个角色就要改测试。
        self.assertIn("999888", reply)
        self.assertTrue(self.memory.qq_blocked("999888"))
        cfg = config.load_config()
        self.assertIn("999888", [str(x) for x in cfg["channels"]["napcat"]["blacklisted_user_ids"]])

        # 取消拉黑
        reply_unblock = policy.owner_command("/取消拉黑 999888", event={"user_id": 1})
        self.assertIn("999888", reply_unblock)
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

    def test_image_wins_by_its_whole_tag_set_not_one_tag(self):
        """两张图都带「可爱」，但只有一张的其余标签也在语境里 —— 该发它。

        这正是老实现最典型的错配：两张图在「可爱」这个标签下完全等价，
        靠 use_count + RANDOM 抽一张，抽到哪张全看运气。
        """
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["可爱", "大笑", "开心"])
            warm = self._add(manager, JPEG_BYTES, ["可爱", "抱抱", "委屈"])
            # 语境是"我不开心 + 抱抱"，所以抱抱/委屈高分、大笑/开心接近 0
            scores = {"可爱": 0.55, "抱抱": 0.92, "委屈": 0.70,
                      "大笑": 0.02, "开心": 0.01, "none": 0.05}

            with patch.object(JevClient, "score_meme_tags", return_value=scores):
                self.assertEqual(manager.choose_meme("我不开心", "抱抱，怎么啦"), warm)

    def test_position_decay_keeps_long_tag_lists_from_winning(self):
        """标签多的图不能靠"凑数"赢：首位满权重，往后依次衰减。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            # 8 个标签、每个都弱沾边
            self._add(manager, PNG_BYTES,
                      ["开心", "大笑", "卖萌", "可爱", "猫", "狗", "贴贴", "抱抱"])
            exact = self._add(manager, JPEG_BYTES, ["委屈", "叹气"])
            scores = {"委屈": 0.95, "叹气": 0.8, "抱抱": 0.2, "开心": 0.2,
                      "大笑": 0.2, "卖萌": 0.2, "可爱": 0.2, "猫": 0.2,
                      "狗": 0.2, "贴贴": 0.2, "none": 0.05}

            with patch.object(JevClient, "score_meme_tags", return_value=scores):
                self.assertEqual(manager.choose_meme("好累啊", "歇会儿吧"), exact)

    def test_none_score_suppresses_the_meme(self):
        """Jev 说"这条不该发表情"时不发，哪怕某个标签分并不算低。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["点赞", "开心"])

            with patch.object(JevClient, "score_meme_tags",
                              return_value={"点赞": 0.48, "开心": 0.42, "none": 0.92}):
                self.assertIsNone(manager.choose_meme("转好了吗", "转好了，在 D 盘"))

    def test_low_scores_stay_below_the_floor(self):
        """全部标签都只是弱沾边 -> 不发（宁缺毋滥）。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["疑惑"])

            with patch.object(JevClient, "score_meme_tags",
                              return_value={"疑惑": 0.30, "none": 0.10}):
                self.assertIsNone(manager.choose_meme("这报错啥意思", "参数类型不对"))

    def test_jev_failure_falls_back_to_primary_tag_order(self):
        """Jev 抛错就退回首位标签排序，而不是不发或乱发。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["疑惑", "卖萌"])
            main = self._add(manager, JPEG_BYTES, ["卖萌", "疑惑"])

            with patch.object(JevClient, "score_meme_tags", side_effect=RuntimeError("boom")):
                self.assertEqual(manager.choose_meme("在吗", "别卖萌了"), main)

    def test_choose_meme_respects_jev_verdict(self):
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, PNG_BYTES, ["卖萌", "猫"])

            with patch.object(JevClient, "score_meme_tags", return_value=None):
                self.assertIsNone(manager.choose_meme("在吗", "喵～在的"))   # 无字面命中 -> 不发
            with patch.object(JevClient, "score_meme_tags",
                              return_value={"卖萌": 0.9, "none": 0.05}):
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
                with patch.object(JevClient, "score_meme_tags",
                              return_value={"卖萌": 0.9, "none": 0.05}):
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

            with patch.object(JevClient, "score_meme_tags",
                              return_value={"卖萌": 0.9, "none": 0.05}):
                first = manager.choose_meme("在吗", "喵～在的", chat_key="group:1")
                manager.note_sent("group:1", first)
                again_other_chat = manager.choose_meme("在吗", "喵～在的", chat_key="group:2")
            self.assertIn(again_other_chat, {path_a, path_b})   # 别的会话不受冷却影响

    def test_cooled_image_is_skipped_even_when_it_scores_highest(self):
        """冷却在选图这一刻生效：分再高也跳过，顺位取下一张。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            liked = self._add(manager, PNG_BYTES, ["赞同"])
            laugh = self._add(manager, JPEG_BYTES, ["大笑"])
            manager.note_sent("group:1", liked)          # 这张刚发过

            with patch.object(JevClient, "score_meme_tags",
                              return_value={"赞同": 0.9, "大笑": 0.6, "none": 0.05}):
                picked = manager.choose_meme("在吗", "喵～在的", chat_key="group:1")
            self.assertEqual(picked, laugh)               # 赞同分更高但在冷却

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


class SelectMemeScoreTests(Fixture):
    """标签独立打分：一次请求覆盖一批，跨批合并，失败不拖垮已拿到的分。"""

    def _client(self):
        from core.jev import JevClient

        return JevClient(base_url="http://127.0.0.1:1", api_key="test-key")

    @staticmethod
    def _flat(questions, value=0.5):
        return {"answers": {name: {"type": "noul", "noul": value} for name in questions}}

    def test_scores_every_tag_and_none_in_one_request(self):
        client = self._client()
        seen = []

        def fake_decide(state, questions, images=None):
            seen.append(questions)
            return self._flat(questions)

        client.decide_raw = fake_decide
        scores = client.score_meme_tags("在吗", "在的", ["卖萌", "可爱", "猫"])
        self.assertEqual(len(seen), 1)                      # 小图库一次问完，不拆批
        self.assertEqual(set(scores), {"卖萌", "可爱", "猫", "none"})
        self.assertEqual(len(seen[0]), 4)                   # 3 个标签 + none

    def test_splits_by_the_server_question_limit(self):
        """单请求最多 64 个 question，所以一批装 63 个标签 + none。"""
        client = self._client()
        sizes = []

        def fake_decide(state, questions, images=None):
            sizes.append(len(questions))
            return self._flat(questions, 0.4)

        client.decide_raw = fake_decide
        scores = client.score_meme_tags("在吗", "在的", [f"t{i}" for i in range(130)])
        self.assertEqual(sizes, [64, 63, 4])                # 130 = 63 + 63 + 4
        self.assertEqual(len(scores), 131)                  # 130 个标签 + none

    def test_respects_the_batch_cap(self):
        client = self._client()
        sizes = []

        def fake_decide(state, questions, images=None):
            sizes.append(len(questions))
            return self._flat(questions, 0.4)

        client.decide_raw = fake_decide
        scores = client.score_meme_tags(
            "在吗", "在的", [f"t{i}" for i in range(300)], max_batches=2)
        self.assertEqual(sizes, [64, 63])                   # 不再往后问
        self.assertEqual(len(scores), 127)                  # 126 个标签 + none

    def test_keeps_the_first_batch_when_a_later_one_fails(self):
        client = self._client()
        calls = []

        def fake_decide(state, questions, images=None):
            calls.append(1)
            if len(calls) > 1:
                raise TimeoutError("network down")
            return self._flat(questions, 0.7)

        client.decide_raw = fake_decide
        scores = client.score_meme_tags(
            "在吗", "在的", [f"t{i}" for i in range(130)], max_batches=3)
        self.assertIsNotNone(scores)                        # 第一批的分不白拿
        self.assertEqual(len(scores), 64)                   # 63 个标签 + none
        self.assertEqual(len(calls), 2)                     # 第二批失败就收手，不硬撞第三批

    def test_gives_up_when_the_first_batch_fails(self):
        client = self._client()

        def fake_decide(state, questions, images=None):
            raise TimeoutError("network down")

        client.decide_raw = fake_decide
        self.assertIsNone(client.score_meme_tags("在吗", "在的", ["卖萌", "可爱"]))

    def test_clamps_out_of_range_scores(self):
        """noul 缺字段、越界、非数字都夹到 0~1，不能让脏值污染加权。"""
        client = self._client()

        def fake_decide(state, questions, images=None):
            values = [None, 1.7, -0.3, "x"]
            answers = {}
            for i, name in enumerate(questions):
                answers[name] = {"type": "noul", "noul": values[i % len(values)]}
            return {"answers": answers}

        client.decide_raw = fake_decide
        scores = client.score_meme_tags("在吗", "在的", ["卖萌", "可爱", "猫", "狗"])
        for value in scores.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_returns_none_without_configured_key(self):
        from core.jev import JevClient

        client = JevClient(base_url="http://127.0.0.1:1", api_key="")
        client.decide_raw = lambda *a, **k: self.fail("没有 key 就不该发请求")
        self.assertIsNone(client.score_meme_tags("在吗", "在的", ["卖萌"]))

if __name__ == "__main__":
    unittest.main()
