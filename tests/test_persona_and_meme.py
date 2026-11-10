import unittest
import asyncio
import base64
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
            with patch.object(channel, "_send", new_callable=AsyncMock, return_value=True) as send, patch("channels.napcat_channel.asyncio.sleep", new_callable=AsyncMock):
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
# 真正能解码的 1×1 PNG：MediaStore 会校验图片可解析，假头字节过不了入库。
REAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


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

    def test_chat_key_from_scope_matches_the_channel_wording(self):
        """channel 记的是 group:123，工具层拿到的是 qq_group_123；对不上就永远找不到。"""
        from core.meme import chat_key_from_scope

        self.assertEqual(chat_key_from_scope("qq_group_123"), "group:123")
        self.assertEqual(chat_key_from_scope("qq:group:123"), "group:123")
        self.assertEqual(chat_key_from_scope("qq_private_456"), "user:456")
        self.assertEqual(chat_key_from_scope("qq:group:123:user:9"), "group:123")
        self.assertEqual(chat_key_from_scope("main"), "")

    def test_forget_last_drops_record_file_and_tag(self):
        """「刚发的那张删掉」：记录、文件、标签汇总一起清，且不许再被采集回来。"""
        from core.meme import MemeManager

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            path = self._add(manager, PNG_BYTES, ["卖萌"])
            manager.note_sent("group:1", path)

            result = manager.forget_last("group:1")

            self.assertTrue(result["ok"])
            self.assertEqual(result["matched_by"], "path")
            self.assertTrue(result["file_removed"])
            self.assertFalse(Path(path).exists())
            self.assertEqual(manager.stats()["total"], 0)
            self.assertEqual({item["tag"] for item in manager.tag_summary()}, set())
            # 只删不拦，群友把它再发一遍就自己长回来了
            self.assertIsNone(manager.add_from_bytes(PNG_BYTES, "test"))
            # 冷却队列也要摘掉，否则"刚发的那张"指向一个已经不存在的文件
            self.assertEqual(manager.last_sent("group:1"), "")

    def test_forget_by_image_leaves_other_memes_alone(self):
        from core.meme import MemeManager

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            keep = self._add(manager, PNG_BYTES, ["卖萌"])
            drop = self._add(manager, GIF_BYTES, ["大笑"])

            result = manager.forget_by_image(GIF_BYTES, "这张别发了")

            self.assertTrue(result["ok"])
            self.assertEqual(result["matched_by"], "hash")
            self.assertFalse(Path(drop).exists())
            self.assertTrue(Path(keep).exists())
            self.assertEqual(manager.stats()["total"], 1)

    def test_forget_by_image_recognises_a_reencoded_copy(self):
        """保存后重发会重新编码，字节全变；按画面认才删得掉。"""
        try:
            import io

            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("需要 Pillow 才能验证画面比对")

        def art(seed):
            image = Image.new("RGB", (120, 120), (240, 240, 240))
            draw = ImageDraw.Draw(image)
            for i in range(6):
                draw.line([(0, i * seed * 7 % 120), (120, i * seed * 13 % 120)],
                          fill=(i * seed * 37 % 255, i * seed * 61 % 255, i * seed * 97 % 255),
                          width=5)
            return image

        from core.meme import MemeManager

        original, recoded = io.BytesIO(), io.BytesIO()
        art(3).save(original, format="PNG")
        art(3).save(recoded, format="JPEG", quality=68)
        self.assertNotEqual(original.getvalue(), recoded.getvalue())

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            self._add(manager, original.getvalue(), ["卖萌"])

            result = manager.forget_by_image(recoded.getvalue(), "这张别发了")

            self.assertTrue(result["ok"])
            self.assertEqual(result["matched_by"], "phash")
            self.assertEqual(manager.stats()["total"], 0)
            # 同一个重编码版本再发回来同样进不来
            self.assertIsNone(manager.add_from_bytes(recoded.getvalue(), "test"))

    def test_forget_by_image_never_trusts_a_degenerate_fingerprint(self):
        """纯色图的 dHash 全是 0，两张不同颜色的图指纹一样；宁可删不掉也不能删错。"""
        try:
            import io

            from PIL import Image
        except ImportError:
            self.skipTest("需要 Pillow 才能验证画面比对")

        def solid(color):
            buffer = io.BytesIO()
            Image.new("RGB", (80, 80), color).save(buffer, format="PNG")
            return buffer.getvalue()

        from core.meme import MemeManager

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            manager.add_from_bytes(solid((200, 20, 20)), "test")

            result = manager.forget_by_image(solid((20, 20, 200)), "别的颜色")

            self.assertEqual(result["matched_by"], "none")
            self.assertEqual(manager.stats()["total"], 1)

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

    def test_low_scores_still_send_a_meme(self):
        """分数低不再拦发布：发不发只由 none 决定，标签分只影响选哪张。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            showed = self._add(manager, PNG_BYTES, ["疑惑"])

            with patch.object(JevClient, "score_meme_tags",
                              return_value={"疑惑": 0.30, "none": 0.10}):
                self.assertEqual(manager.choose_meme("这报错啥意思", "参数类型不对"), showed)

    def test_matches_with_any_of_the_top_three_tags(self):
        """只命中 Jev 排第三的标签也要发——旧实现只认第一名，这里会整轮沉默。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            third = self._add(manager, JPEG_BYTES, ["委屈", "叹气"])

            with patch.object(JevClient, "score_meme_tags",
                              return_value={"可爱": 0.92, "抱抱": 0.88, "委屈": 0.81,
                                            "none": 0.05}):
                self.assertEqual(manager.choose_meme("我不开心", "抱抱，怎么啦"), third)

    def test_first_ranked_tag_outweighs_the_backups(self):
        """前三名都参与，但第一名权重最高：平分时不该被备选标签翻盘。"""
        from core.meme import MemeManager
        from core.jev import JevClient

        with patch.object(MemeManager, "_ensure_tagger", lambda self: None):
            manager = self._manager()
            primary = self._add(manager, PNG_BYTES, ["抱抱", "委屈"])
            backup = self._add(manager, JPEG_BYTES, ["可爱"])

            # 抱抱第一、委屈第二、可爱第三：前两张图都只靠自己名字里的标签
            with patch.object(JevClient, "score_meme_tags",
                              return_value={"抱抱": 0.85, "委屈": 0.84, "可爱": 0.83,
                                            "none": 0.05}):
                self.assertEqual(manager.choose_meme("别难过", "抱抱你"), primary)

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


class MemeAfterTextTests(Fixture):
    """表情包在正文发完之后单独发一条，正文不出现任何 media:// 占位。"""

    def _manager(self):
        from core import meme as meme_module

        with patch.object(meme_module.MemeManager, "_ensure_tagger", lambda self: None):
            return meme_module.MemeManager(meme_dir=self.root / "memes",
                                           db_path=self.root / "memes.db")

    def _patched(self, manager, picked):
        from core import meme as meme_module

        return (
            patch.object(meme_module, "get_meme_manager", return_value=manager),
            patch.object(meme_module.MemeManager, "choose_meme", return_value=picked),
        )

    def test_pick_returns_ref_in_scope_without_touching_text(self):
        from core.meme import pick_meme_asset
        from core.media_store import MediaStore

        manager = self._manager()
        path = manager.add_from_bytes(REAL_PNG, "test")
        self.assertIsNotNone(path)

        pick_patch, score_patch = self._patched(manager, path)
        with pick_patch, score_patch:
            picked = pick_meme_asset("主人辛苦啦喵～", "我不开心", "qq:group:123", "group:123")

        self.assertTrue(picked["ref"].startswith("img_"))
        # 占位符只在发送时按 media_ref 临时拼，不会回写到正文
        self.assertNotIn("media://", picked.get("path", ""))
        asset, _ = MediaStore().get(picked["ref"], "qq:group:123")
        self.assertEqual(asset["media_ref"], picked["ref"])
        # 冷却记在挑中时：下一轮不会重复挑同一张
        self.assertIn(path, manager._cooldown_paths("group:123"))

    def test_pick_skips_failure_text_and_replies_that_already_carry_media(self):
        from core.meme import pick_meme_asset

        manager = self._manager()
        path = manager.add_from_bytes(REAL_PNG, "test")
        pick_patch, score_patch = self._patched(manager, path)
        with pick_patch, score_patch as score:
            self.assertEqual(pick_meme_asset("【模型接口错误：HTTP 520。】", "在吗", "qq:group:1"), {})
            self.assertEqual(
                pick_meme_asset("已经做好啦\n![图片](media://img_0123456789abcdef)", "在吗", "qq:group:1"), {})
            self.assertEqual(pick_meme_asset("", "在吗", "qq:group:1"), {})
        score.assert_not_called()

    def test_channel_sends_image_as_its_own_message_after_text(self):
        manager = self._manager()
        path = manager.add_from_bytes(REAL_PNG, "test")
        channel = NapCatChannel()
        event = {"message_type": "group", "group_id": 123, "user_id": 7,
                 "_parsed": {"direct_text": "我不开心"}}
        records = []

        with patch.object(channel, "_send_segments", new_callable=AsyncMock, return_value=True) as send, \
                patch("channels.napcat_channel.pick_meme_asset",
                      return_value={"path": path, "ref": "img_0123456789abcdef"}), \
                patch("channels.napcat_channel.record_meme_in_history",
                      side_effect=lambda *a: records.append(a)), \
                patch("channels.napcat_channel.asyncio.sleep", new_callable=AsyncMock), \
                patch("channels.qq_reply._media_ref_segment",
                      return_value={"type": "image", "data": {"file": "file:///tmp/meme.png"},
                                    "_media": {"media_ref": "img_0123456789abcdef"}}):
            asyncio.run(channel._send_meme_after_text(event, "主人辛苦啦喵～"))

        # 一条独立的图片消息，不是拼在正文里的链接
        segments = send.call_args.args[2]
        self.assertEqual([s["type"] for s in segments], ["image"])
        self.assertNotIn("media://", str(segments))
        self.assertEqual(send.call_args.kwargs["asset_scope"], "qq:group:123")
        # 图片发出去了才记历史，role=assistant
        self.assertEqual(records, [("qq:group:123", "img_0123456789abcdef")])

    def test_meme_is_sent_at_most_once_per_turn(self):
        channel = NapCatChannel()
        event = {"message_type": "private", "user_id": 1}
        with patch.object(channel, "_send_segments", new_callable=AsyncMock, return_value=True), \
                patch("channels.napcat_channel.pick_meme_asset", return_value={}) as pick, \
                patch("channels.napcat_channel.asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(channel._send_meme_after_text(event, "好呀"))
            asyncio.run(channel._send_meme_after_text(event, "好呀"))
        self.assertEqual(pick.call_count, 1)

    def test_history_record_marks_role_assistant(self):
        from core.meme import record_meme_in_history

        session = "qq:group:123"
        with patch("core.memory.MiniMemory", return_value=self.memory):
            self.assertTrue(record_meme_in_history(session, "img_0123456789abcdef"))
        history, _ = self.memory.get_history(session)
        saved = [m for m in history if m.get("role") == "assistant"][-1]
        self.assertEqual(saved["content"], "![图片](media://img_0123456789abcdef)")


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

    def test_rank_meme_tags_returns_top_three(self):
        """Jev 的产出契约：按分排序的前三个标签，而不是一个。"""
        client = self._client()
        client.score_meme_tags = lambda *a, **k: {
            "可爱": 0.4, "抱抱": 0.93, "委屈": 0.71, "大笑": 0.2, "none": 0.04}

        top, _scores = client.rank_meme_tags("我不开心", "抱抱", ["可爱", "抱抱", "委屈", "大笑"])
        self.assertEqual(top, ["抱抱", "委屈", "可爱"])

    def test_rank_meme_tags_sends_when_none_loses(self):
        """分再低也不拦发布：none 没赢就得给三个标签出来。"""
        client = self._client()
        client.score_meme_tags = lambda *a, **k: {"疑惑": 0.3, "无语": 0.2, "none": 0.1}

        top, _scores = client.rank_meme_tags("报错啥意思", "参数不对", ["疑惑", "无语"])
        self.assertEqual(top, ["疑惑", "无语"])

    def test_rank_meme_tags_suppresses_when_none_wins(self):
        """none 得分最高就是「这条别发表情」，不给标签。"""
        client = self._client()
        client.score_meme_tags = lambda *a, **k: {"点赞": 0.48, "开心": 0.42, "none": 0.92}

        top, scores = client.rank_meme_tags("转好了吗", "转好了", ["点赞", "开心"])
        self.assertEqual(top, [])
        self.assertEqual(scores.get("none"), 0.92)

    def test_rank_meme_tags_returns_none_when_unscored(self):
        """拿不到分（未配置/失败）不等于「不该发」，要让调用方走降级。"""
        client = self._client()
        client.score_meme_tags = lambda *a, **k: None

        self.assertIsNone(client.rank_meme_tags("在吗", "在的", ["卖萌"]))

if __name__ == "__main__":
    unittest.main()
