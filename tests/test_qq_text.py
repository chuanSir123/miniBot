"""出站文本整理的测试。

重点是**配置驱动**这个性质：同一份代码，换一套 persona.style 就该换一套行为。
如果哪天有人又把人设相关的词写回 channels/qq_text.py，跨语言那条用例会先失败。
"""

import unittest

from channels.qq_text import TextStyle

CN_STYLE = {
    "persona": {
        "style": {
            "max_kaomoji": 1,
            "max_emoji": 1,
            "courtesy_tail": ["想让本喵帮忙", "随时叫我", "希望对你"],
            "tool_preamble": ["查回来啦", "我挑了几个"],
        }
    }
}

EN_STYLE = {
    "persona": {
        "style": {
            "max_kaomoji": 0,
            "max_emoji": 0,
            "courtesy_tail": ["help you with", "let me know"],
            "tool_preamble": ["Let me check", "Here is what I found"],
        }
    }
}


class TextStyleConfigTests(unittest.TestCase):
    def test_empty_style_leaves_text_untouched(self):
        """不配置风格规则时，除了剥 Markdown 什么都不做。"""
        style = TextStyle.from_config({"persona": {"style": {}}})
        text = "Sure! Let me check. Anything else I can help you with?"
        self.assertEqual(style.tidy_tool_reply(text), text)

    def test_missing_config_does_not_crash(self):
        style = TextStyle.from_config(None)
        self.assertEqual(style.courtesy_tail, ())
        self.assertEqual(style.tidy("普通的一句话"), "普通的一句话")

    def test_unknown_keys_are_ignored(self):
        style = TextStyle.from_config({"persona": {"style": {"没见过的键": 1, "segment_cap": "bad"}}})
        self.assertEqual(style.segment_cap, 6)

    def test_max_kaomoji_can_be_unlimited(self):
        style = TextStyle.from_config({"persona": {"style": {"max_kaomoji": None, "max_emoji": None}}})
        text = "好呀 (๑>◡<๑) (ฅ´ω`ฅ) 🐾🐾"
        self.assertEqual(style.tidy(text), text)


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.style = TextStyle.from_config({"persona": {"style": {}}})

    def test_bold_and_heading_stripped(self):
        out = self.style.tidy("**身份升级**\n# 标题\n他现在统管三块。")
        self.assertNotIn("*", out)
        self.assertNotIn("#", out)
        # 小标题转成「标题，正文」，用逗号而不是冒号——冒号的简报感太重。
        self.assertIn("身份升级，", out)
        self.assertIn("他现在统管三块", out)

    def test_sibling_titles_keep_paragraph_break(self):
        """几条小标题不能粘成一大段。"""
        out = self.style.tidy("**军令状**\n每天发改进。\n\n**流量**\n访问量第一。")
        self.assertIn("\n\n", out)
        self.assertNotIn("：", out)

    def test_table_turns_into_separators(self):
        out = self.style.tidy("| 名称 | 值 |\n| --- | --- |\n| 版本 | 1.2 |")
        self.assertNotIn("|", out)
        self.assertIn("版本", out)

    def test_strip_markdown_can_be_disabled(self):
        style = TextStyle.from_config({"persona": {"style": {"strip_markdown": False}}})
        self.assertIn("**", style.tidy("**加粗**"))


class CourtesyTailTests(unittest.TestCase):
    """句末客套的判定完全依赖配置短语，没有内置的任何问句/动词词表。"""

    def setUp(self):
        self.style = TextStyle.from_config(CN_STYLE)

    def test_trims_configured_closing(self):
        out = self.style.tidy("主人刚清完记忆，喵喵又回来啦，有什么想让本喵帮忙的嘛？")
        self.assertEqual(out, "主人刚清完记忆，喵喵又回来啦")

    def test_keeps_sentence_before_the_courtesy(self):
        out = self.style.tidy("晚安啦，明天见～有需要随时叫我")
        self.assertEqual(out, "晚安啦，明天见")

    def test_unconfigured_question_is_not_touched(self):
        text = "有什么好吃的推荐吗？"
        self.assertEqual(self.style.tidy(text), text)

    def test_whole_message_courtesy_is_kept(self):
        """整条都是客套时不能清空，否则会发出空消息。"""
        text = "随时叫我"
        self.assertEqual(self.style.tidy(text), text)

    def test_statement_signoff(self):
        out = self.style.tidy("这个方案能帮你省不少时间，希望对你有帮助。")
        self.assertEqual(out, "这个方案能帮你省不少时间")


class PreambleTests(unittest.TestCase):
    def setUp(self):
        self.style = TextStyle.from_config(CN_STYLE)

    def test_trims_layered_preamble(self):
        text = "主人，查回来啦～ (๑>◡<๑) 我挑了几个：他现在统管 ChatGPT、Codex 和开发者 API。"
        out = self.style.tidy_tool_reply(text)
        self.assertTrue(out.startswith("他现在统管"))
        self.assertNotIn("查回来啦", out)
        self.assertNotIn("我挑了几个", out)

    def test_does_not_bite_into_content(self):
        text = "查到了这个方法不行，得换个思路"
        self.assertEqual(self.style.tidy_tool_reply(text), text)

    def test_cutting_at_comma_is_refused(self):
        """切在逗号上会得到半句开头，宁可不切。"""
        style = TextStyle.from_config({"persona": {"style": {"tool_preamble": ["Sure"]}}})
        text = "Sure, here you go. The build broke because the port was taken."
        self.assertEqual(style.tidy_tool_reply(text), text)


class LanguageAgnosticTests(unittest.TestCase):
    """同一份代码，只改配置就能处理另一种语言/人设。"""

    def setUp(self):
        self.style = TextStyle.from_config(EN_STYLE)

    def test_english_preamble(self):
        text = "Let me check. Here is what I found. The build broke because port 3001 was taken."
        self.assertEqual(self.style.tidy_tool_reply(text), "The build broke because port 3001 was taken.")

    def test_english_courtesy_tail(self):
        text = "The build broke because port 3001 was taken. Let me know if it happens again."
        out = self.style.tidy_tool_reply(text)
        self.assertNotIn("let me know", out.lower())
        self.assertIn("port 3001", out)

    def test_filenames_are_not_split(self):
        text = "Edit main.py and set the port to 3001. Then restart."
        self.assertEqual(self.style.tidy_tool_reply(text), text)


class DecorationTests(unittest.TestCase):
    def test_limits_kaomoji(self):
        style = TextStyle.from_config({"persona": {"style": {"max_kaomoji": 1, "max_emoji": 0}}})
        out = style.tidy("好呀 (๑>◡<๑) 真的 (ฅ´ω`ฅ)")
        self.assertEqual(out.count("(๑>◡<๑)"), 1)
        self.assertNotIn("(ฅ´ω`ฅ)", out)

    def test_keeps_non_kaomoji_parens(self):
        """括号里只要有文字或数字就不是颜文字，不能删。"""
        style = TextStyle.from_config({"persona": {"style": {"max_kaomoji": 0}}})
        out = style.tidy("多久到公司 (2024) （注：明天） (说明)")
        self.assertIn("(2024)", out)
        self.assertIn("（注：明天）", out)
        self.assertIn("(说明)", out)


class SegmentationTests(unittest.TestCase):
    def setUp(self):
        self.style = TextStyle.from_config({"persona": {"style": {}}})

    def test_single_sentence_is_one_part(self):
        self.assertEqual(self.style.parts_for("在呀，主人找我什么事喵～"), 1)

    def test_two_short_sentences_still_split(self):
        """短短两句也该分两条气泡，否则反而不像人在打字。"""
        self.assertEqual(self.style.parts_for("你好。很高兴见到你！"), 2)

    def test_long_text_scales_with_length(self):
        """长文本按 chunk 加段数，不写成常数——chunk 是可配的。"""
        style = TextStyle.from_config({"persona": {"style": {"segment_chunk": 100, "segment_cap": 6}}})
        self.assertEqual(style.parts_for("x" * 400), 4)
        self.assertEqual(style.parts_for("x" * 250), 3)

    def test_capped(self):
        self.assertEqual(self.style.parts_for("x" * 5000), 6)


if __name__ == "__main__":
    unittest.main()
