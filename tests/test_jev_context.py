"""Jev 各调用点的上下文瘦身回归测试。

背景：steering 判定曾把整份未压缩 task_state（真实 web 会话 110KB）发给 Jev，
实测 8.9s；换成任务摘要 + 最近 10 条消息后 0.85s。这里锁住"只带该带的"，
防止以后又有人把完整 task_state 或媒体清单塞回去。
"""
import json
import unittest
from unittest.mock import MagicMock, patch

from test_core import Fixture

from core.decision_context import (BRIEF_TASK_KEYS, build_decision_context,
                                   lite_context)
from core.harness import AgentHarness
from core.jev import JevClient

HEAVY = ("recent_media", "conversation_summary", "recent_chat", "recent_conversation",
         "memory", "goals", "pending_reminders")


def bloated_task_state():
    return {
        "original_goal": "把页面改成动画",
        "latest_request": "再快一点",
        "status": "unverified",
        "scope": "local",
        "verification": {"mode": "dynamic"},
        "file_changes": [{"path": f"f{i}.html", "added": 10, "removed": 1, "diff": "d" * 12000}
                         for i in range(10)],
        "last_tools": [{"tool": "write_file", "ok": True, "result_preview": "x" * 800}
                       for _ in range(50)],
        "artifacts": ["a"] * 40,
    }


class LiteContextTests(Fixture):
    def test_lite_drops_heavy_branches_but_keeps_identity(self):
        self.memory.save_task_state("local:pet", bloated_task_state())
        full = build_decision_context(self.memory, "local:pet", "local")
        lite = build_decision_context(self.memory, "local:pet", "local", lite=True)
        for key in HEAVY:
            self.assertNotIn(key, lite)
        # 身份与权限是判定授权的前提，任何档位都不能丢。
        for key in ("owner_ids", "permissions", "sender" if "sender" in full else "session_id"):
            self.assertIn(key, lite)
        self.assertLess(len(json.dumps(lite, ensure_ascii=False)),
                        len(json.dumps(full, ensure_ascii=False)))

    def test_lite_task_state_keeps_outcome_not_diff(self):
        self.memory.save_task_state("local:pet", bloated_task_state())
        lite = build_decision_context(self.memory, "local:pet", "local", lite=True)
        state = lite["task_state"]
        self.assertEqual(set(state), set(BRIEF_TASK_KEYS) & set(bloated_task_state()))
        self.assertNotIn("file_changes", state)
        self.assertNotIn("last_tools", state)

    def test_lite_context_of_dict_is_derived_without_requery(self):
        full = {"session_id": "s", "recent_media": [1], "memory": "m",
                "task_state": {"original_goal": "g", "file_changes": [1]}}
        lite = lite_context(full)
        self.assertNotIn("recent_media", lite)
        self.assertNotIn("memory", lite)
        self.assertEqual(lite["task_state"], {"original_goal": "g"})
        # 必须返回新对象，不能把调用方的完整 context 就地改掉。
        self.assertIn("recent_media", full)


class HarnessHelpersTests(unittest.TestCase):
    def test_recent_messages_keeps_only_talk_and_limits(self):
        history = [{"role": "system", "content": "规则"}]
        for i in range(30):
            history.append({"role": "user", "content": f"u{i}"})
            history.append({"role": "assistant", "content": None, "tool_calls": [{}]})
            history.append({"role": "tool", "content": "结果"})
        msgs = AgentHarness._recent_messages(history)
        self.assertEqual(len(msgs), 10)
        self.assertEqual([m["role"] for m in msgs], ["user"] * 10)
        self.assertEqual(msgs[-1]["text"], "u29")

    def test_task_brief_drops_diff_and_tool_log(self):
        brief = AgentHarness._task_brief(bloated_task_state())
        self.assertNotIn("file_changes", brief)
        self.assertNotIn("last_tools", brief)
        self.assertNotIn("artifacts", brief)
        self.assertEqual(brief["original_goal"], "把页面改成动画")


class SteeringPayloadTests(Fixture):
    def test_steering_sends_brief_and_at_most_ten_messages(self):
        client = JevClient()
        captured = {}

        def fake_decide(state, questions, images=None):
            captured["state"] = json.loads(state)
            return {"intent_type": "steering"}

        with patch.object(client, "decide", side_effect=fake_decide):
            messages = [{"role": "user", "text": f"m{i}"} for i in range(25)]
            client.classify_steering_intent(
                "再快一点",
                task_state=AgentHarness._task_brief(bloated_task_state()),
                context={"session_id": "s"},
                messages=messages,
            )
        state = captured["state"]["current"]
        self.assertEqual(len(state["recent_messages"]), 10)
        self.assertEqual(state["recent_messages"][-1]["text"], "m24")
        self.assertNotIn("file_changes", state["current_or_last_task"])
        self.assertLess(len(json.dumps(state, ensure_ascii=False)), 4000)

    def test_steering_without_context_sends_no_background(self):
        """最近十条对话已经说明上下文承接，再背一份 context 是重复的。"""
        client = JevClient()
        captured = {}

        def fake_decide(state, questions, images=None):
            captured["state"] = json.loads(state)
            return {"intent_type": "independent_task"}

        with patch.object(client, "decide", side_effect=fake_decide):
            client.classify_steering_intent(
                "再快一点",
                task_state=AgentHarness._task_brief(bloated_task_state()),
                messages=[{"role": "user", "text": "上一轮改了什么"}],
            )
        self.assertEqual(captured["state"]["context"], {})
        self.assertLess(len(json.dumps(captured["state"], ensure_ascii=False)), 1200)


class NoOpeningContractTests(unittest.TestCase):
    def test_harness_keeps_no_judgement_calls(self):
        """进入 harness 之后不再有任何判定调用。

        交付是否完成由程序的确定性门禁按本轮真实改动核对；Jev 只在进入 harness
        之前做两件事：判断这句话是继续还是新任务，以及能不能用预设动作直接满足。
        放行判定与技能路由都已经拆掉，保留断言是为了防止它们被重新加回来。
        """
        for gone in ("plan_verification", "evaluate_task_completion", "classify_progress_delivery",
                     "final_review", "route_request"):
            self.assertFalse(hasattr(JevClient, gone), gone)
        self.assertTrue(hasattr(JevClient, "classify_steering_intent"))
        self.assertTrue(hasattr(JevClient, "select_fixed_action"))
        for gone in ("_valid_plan", "_visual_evidence", "_gap_feedback", "_program_checks"):
            self.assertFalse(hasattr(AgentHarness, gone), gone)


class ConfidenceSemanticsTests(unittest.TestCase):
    def test_margin_is_used_when_server_omits_confidence(self):
        """服务端的 confidence 是第一名减第二名；缺字段时也必须算差值。

        如果退回 max(probabilities)，同一份 0.65/0.35 的回答会从 0.30 跳到 0.65，
        跨过 0.6 的采信门槛——一次抛硬币的结果就成了"已核验通过"。
        """
        self.assertAlmostEqual(
            JevClient._answer_confidence({"probabilities": {"pass": 0.6502, "reject": 0.3497}}),
            0.3005, places=4)
        self.assertEqual(
            JevClient._answer_confidence({"confidence": 0.93,
                                          "probabilities": {"pass": 0.96, "reject": 0.04}}), 0.93)
        self.assertEqual(JevClient._answer_confidence({}), 1.0)


class QQGatePayloadTests(Fixture):
    def test_gate_context_is_recent_lines_only(self):
        """群消息门控只看最近十条来往，不背媒体清单与长期记忆。"""
        from core.qq_policy import QQPolicy
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.decide_raw.return_value = {"answers": {"reply": {"noul": 0.9},
                                                   "moderation": {"choice": "allow"}}}
        policy = QQPolicy(self.memory, jev)
        for i in range(15):
            event = {"message_type": "group", "group_id": 88, "user_id": 100 + i,
                     "self_id": 999, "message_id": i,
                     "sender": {"nickname": f"u{i}", "card": ""}}
            policy.observe(f"第{i}句", event)
        event = {"message_type": "group", "group_id": 88, "user_id": 123, "self_id": 999,
                 "message_id": 99, "sender": {"nickname": "传哥", "card": ""},
                 "_parsed": {"direct_text": "在吗"}}
        policy.decide_reply("在吗", event)
        state = jev.decide_raw.call_args.args[0]
        # 键顺序是契约，不是实现细节：常量在前、当前消息垫底。
        # 服务端把 state 当每个 question 的共享前缀，顺序反了前缀就永远分叉。
        self.assertEqual(list(state), ["bot_self_qq", "persona_names", "recent_chat", "current_message"])
        self.assertIn("called_persona_name", state["current_message"])
        self.assertEqual(len(state["recent_chat"]), 10)
        self.assertEqual(state["recent_chat"][-1]["text"], "第14句")
        self.assertLess(len(json.dumps(state, ensure_ascii=False)), 4000)


class QQGateAtTargetTests(Fixture):
    """门控的输入与 @ 判定。

    实测：同一条消息在 recent_chat 里重复出现一次，会把 reply 的概率从
    0.68 压到 0.16；而 "@ 了别人" 这个确定性信号原本交给模型去比 qq 数字，
    分数在 0.0756/0.1793/0.2663 之间飘。这两条都不能回退。
    """

    BOT, OWNER, OTHER = 999, 123, 555

    def policy(self, answers=None):
        from core.qq_policy import QQPolicy
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.decide_raw.return_value = answers or {"answers": {"moderation": {"choice": "allow"}}}
        return QQPolicy(self.memory, jev), jev

    def event(self, text, message_id=1, parsed=None):
        return {"message_type": "group", "group_id": 88, "user_id": self.OWNER,
                "self_id": self.BOT, "message_id": message_id,
                "sender": {"nickname": "传哥", "card": ""},
                "_parsed": parsed or {"direct_text": text}}

    def test_current_message_is_not_repeated_in_history(self):
        policy, jev = self.policy({"answers": {"reply": {"noul": 0.9},
                                               "moderation": {"choice": "allow"}}})
        policy.observe("上一句", self.event("上一句", message_id=1))
        current = self.event("每天早上 9 点给我点赞 50 次", message_id=2)
        policy.observe(current["_parsed"]["direct_text"], current)  # 线上顺序：先 observe 再判定
        policy.decide_reply("每天早上 9 点给我点赞 50 次", current)
        state = jev.decide_raw.call_args.args[0]
        texts = [item["text"] for item in state["recent_chat"]]
        self.assertEqual(texts, ["上一句"])
        # 没有引用时不该再发一份原文，否则同一句话被看成两条
        self.assertNotIn("text_with_quote", state["current_message"])

    def test_at_other_member_is_judged_by_jev_with_at_context(self):
        """@ 别人不写死在代码里：交给 Jev，但要给足 at_targets 与本机号码。"""
        policy, jev = self.policy({"answers": {"reply": {"noul": 0.02},
                                               "moderation": {"choice": "allow"}}})
        parsed = {"direct_text": f"[@{self.OTHER}] 每天早上 9 点 给我点赞 50 次",
                  "at_targets": [{"qq": str(self.OTHER), "name": "绵羊"}]}
        self.assertFalse(policy.decide_reply(parsed["direct_text"],
                                             self.event(parsed["direct_text"], parsed=parsed)))
        questions = jev.decide_raw.call_args.args[1]
        self.assertIn("reply", questions)             # 由 Jev 判，不短路
        state = jev.decide_raw.call_args.args[0]
        self.assertEqual(state["bot_self_qq"], str(self.BOT))
        self.assertFalse(state["current_message"]["at_targets"][0]["is_this_bot"])
        self.assertIn("绵羊", json.dumps(state, ensure_ascii=False))  # @ 对象带人名，不只 qq 号

    def test_at_other_member_can_still_reply_when_jev_says_so(self):
        policy, _ = self.policy({"answers": {"reply": {"noul": 0.93},
                                             "moderation": {"choice": "allow"}}})
        parsed = {"direct_text": f"[@{self.OTHER}] 这个报错怎么修",
                  "at_targets": [{"qq": str(self.OTHER), "name": "绵羊"}]}
        self.assertTrue(policy.decide_reply(parsed["direct_text"],
                                            self.event(parsed["direct_text"], parsed=parsed)))

    def test_at_other_member_but_persona_name_called_still_asks(self):
        policy, jev = self.policy({"answers": {"reply": {"noul": 0.9},
                                               "moderation": {"choice": "allow"}}})
        parsed = {"direct_text": f"[@{self.OTHER}] 喵喵帮我看下这个报错",
                  "at_targets": [{"qq": str(self.OTHER), "name": "绵羊"}]}
        with patch("core.qq_policy.names", return_value=["喵喵"]):
            self.assertTrue(policy.decide_reply(parsed["direct_text"],
                                                self.event(parsed["direct_text"], parsed=parsed)))
        self.assertIn("reply", jev.decide_raw.call_args.args[1])

    def test_at_self_target_is_not_treated_as_other(self):
        policy, jev = self.policy({"answers": {"reply": {"noul": 0.9},
                                               "moderation": {"choice": "allow"}}})
        parsed = {"direct_text": f"[@{self.BOT}] 在吗",
                  "at_targets": [{"qq": str(self.BOT)}]}
        self.assertTrue(policy.decide_reply(parsed["direct_text"],
                                            self.event(parsed["direct_text"], parsed=parsed)))
        self.assertIn("reply", jev.decide_raw.call_args.args[1])

    def test_missing_reply_threshold_falls_back_instead_of_raising(self):
        policy, jev = self.policy({"answers": {"reply": {"noul": 0.75},
                                               "moderation": {"choice": "allow"}}})
        cfg = {"channels": {"napcat": {"owner_ids": [str(self.OWNER)],
                                       "blacklisted_user_ids": []}}}
        with patch("core.qq_policy.load_config", return_value=cfg):
            self.assertTrue(policy.decide_reply("在吗", self.event("在吗")))


class DecideRawPayloadTests(unittest.TestCase):
    def test_structured_state_is_serialized_by_decide_raw(self):
        client = JevClient(api_key="sk-vsr-test", base_url="https://example.invalid/v1")
        sent = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *a):
                return b""

        def fake_open(req, timeout=None):
            sent["body"] = json.loads(req.data)
            raise RuntimeError("stop before parsing")

        with patch("core.jev.open_request", side_effect=fake_open):
            with self.assertRaises(RuntimeError):
                client.decide_raw({"current": {"a": 1}}, {"q": {"type": "noul"}})
        # 传 dict 的调用方不该自己 dumps：序列化由 decide_raw 统一负责。
        self.assertEqual(json.loads(sent["body"]["state"]), {"current": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
