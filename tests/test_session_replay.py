"""会话回放回归测试：切会话时命令被打印两次、记忆条目混进聊天流。

背景：记忆召回（kind=memory）和 Harness 提示（kind=notice）都以 user 身份落库，
因为它们要参与模型投影。但副作用是——

1. 旧代码按"最后一条 role=user"判断用户输入是否已落库，记忆条目紧跟其后，
   于是同一句话被追加第二次，切会话时看到命令重复；
2. Web 端 /api/session/messages 把这些条目原样回显，界面上冒出一大段
   "用户明确记忆 / 近期事件"。

两个都只影响"展示/落库时机"，不能动投影结果。
"""
import unittest

from core.harness import needs_user_prompt_append
from core.transcript import KIND_MEMORY, KIND_NOTICE, memory_entry
from webui.server import _filter_display_messages


class DuplicatePromptTests(unittest.TestCase):
    PROMPT = "写一个 html 一个鹈鹕骑自行车的 svg 动画，转 mp4 发我"

    def _recall(self, facts="用户明确记忆：\n{\"a\": \"b\"}", events=None):
        return {
            "facts": facts,
            "facts_hash": "hash-1",
            "events": events or [{"id": 1, "text": "上一条事件"}],
            "related": [],
        }

    def test_prompt_already_stored_is_not_appended_again(self):
        """Web 端先落了用户输入，记忆条目跟在后面——不该再追加一次。"""
        history = [
            {"role": "user", "content": self.PROMPT},
            memory_entry(self._recall(), None),
        ]
        self.assertFalse(needs_user_prompt_append(history, self.PROMPT))

    def test_memory_entry_alone_does_not_look_like_user_input(self):
        """历史里只有记忆条目时，用户输入仍然需要落库。"""
        history = [memory_entry(self._recall(), None)]
        self.assertTrue(needs_user_prompt_append(history, self.PROMPT))

    def test_notice_entry_does_not_swallow_the_prompt(self):
        history = [
            {"role": "user", "content": self.PROMPT},
            {"role": "user", "content": "【Harness 提示】先验证", "meta": {"kind": KIND_NOTICE}},
        ]
        self.assertFalse(needs_user_prompt_append(history, self.PROMPT))

    def test_different_prompt_still_appends(self):
        history = [{"role": "user", "content": "上一句话"}]
        self.assertTrue(needs_user_prompt_append(history, self.PROMPT))

    def test_memory_entry_is_still_one_entry_with_meta(self):
        """投影要用的 meta 不能被削掉，否则记录变成普通用户发言。"""
        entry = memory_entry(self._recall(), None)
        self.assertEqual(entry["role"], "user")
        self.assertEqual(entry["meta"]["kind"], KIND_MEMORY)
        self.assertIn("用户明确记忆", entry["content"])
        self.assertIn("近期事件", entry["content"])


class DisplayFilterTests(unittest.TestCase):
    def test_internal_entries_are_hidden_from_chat_stream(self):
        messages = [
            {"role": "user", "content": "正常提问", "_id": 1},
            {"role": "user", "content": "用户明确记忆：{...}", "meta": {"kind": KIND_MEMORY}, "_id": 2},
            {"role": "user", "content": "【Harness 提示】", "meta": {"kind": KIND_NOTICE}, "_id": 3},
            {"role": "system", "content": "内部系统提示", "_id": 4},
            {"role": "assistant", "content": "回答", "_id": 5},
            {"role": "tool", "content": "{}", "_id": 6},
        ]
        visible = _filter_display_messages(messages)
        self.assertEqual([m["_id"] for m in visible], [1, 5, 6])

    def test_steering_message_stays_visible(self):
        """真实引导是用户说的话，必须留在聊天流里。"""
        messages = [
            {"role": "user", "content": "【用户实时引导与方向修正】\n换个颜色", "is_steering": True, "_id": 7},
        ]
        self.assertEqual([m["_id"] for m in _filter_display_messages(messages)], [7])


class LegacyDuplicateCollapseTests(unittest.TestCase):
    """库里已经存下的重复命令：回显时折叠，不改数据。"""

    PROMPT = "写一个 html 一个鹈鹕骑自行车的 svg 动画，转 mp4 发我"

    def test_legacy_duplicate_around_memory_entry_is_collapsed(self):
        messages = [
            {"role": "user", "content": self.PROMPT, "_id": 1824},
            {"role": "user", "content": "用户明确记忆：{...}", "meta": {"kind": KIND_MEMORY}, "_id": 1825},
            {"role": "user", "content": self.PROMPT, "_id": 1826},
            {"role": "assistant", "content": "好嘞", "_id": 1830},
        ]
        self.assertEqual([m["_id"] for m in _filter_display_messages(messages)], [1824, 1830])

    def test_real_repeat_after_reply_is_kept(self):
        """用户隔着一句回复又说了一遍同样的话，是真实发言，不能折叠。"""
        messages = [
            {"role": "user", "content": "继续", "_id": 1},
            {"role": "assistant", "content": "好的", "_id": 2},
            {"role": "user", "content": "继续", "_id": 3},
            {"role": "assistant", "content": "好的", "_id": 4},
        ]
        self.assertEqual([m["_id"] for m in _filter_display_messages(messages)], [1, 2, 3, 4])

    def test_three_in_a_row_collapses_to_one(self):
        messages = [
            {"role": "user", "content": "跑测试", "_id": 1},
            {"role": "user", "content": "跑测试", "_id": 2},
            {"role": "user", "content": "跑测试", "_id": 3},
        ]
        self.assertEqual([m["_id"] for m in _filter_display_messages(messages)], [1])


if __name__ == "__main__":
    unittest.main()
