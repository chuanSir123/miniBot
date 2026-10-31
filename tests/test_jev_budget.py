"""Jev 请求体预算与 task_state 瘦身的回归测试。

背景：决策服务对 state 有硬上限 131,072 字符，而真实 web 长任务曾经发到
163,984 字符，直接 400 state_too_long；即使没超限，47KB 的 state 也要 3.6s。
"""
import json
import unittest

from core.decision_context import compact_task_state
from core.jev_budget import MAX_STATE_CHARS, SAFE_STATE_CHARS, fit_state, fit_wire


def big_state(chars=180_000):
    filler = "填充" * (chars // 2)
    return json.dumps({"current": {"current_request": "做一个动画页面", "reply": "完成",
                                   "current_trace": [{"index": i, "tool": "execute_command",
                                                      "result": {"ok": True, "output": "x" * 5000}}
                                                     for i in range(20)]},
                       "context": {"task_state": {"file_changes": [
                           {"path": f"f{i}.html", "added": 100, "removed": 1, "diff": "d" * 12000}
                           for i in range(10)]},
                           "recent_chat": [{"role": "user", "text": filler}]}})


class FitStateTests(unittest.TestCase):
    def test_within_budget_is_untouched(self):
        small = json.dumps({"current": {"a": 1}, "context": {}})
        self.assertEqual(fit_state(small), (small, []))

    def test_oversized_state_is_brought_under_budget(self):
        raw = big_state()
        self.assertGreater(len(raw), MAX_STATE_CHARS)
        fitted, steps = fit_state(raw)
        self.assertLessEqual(len(fitted), SAFE_STATE_CHARS)
        self.assertTrue(steps)
        # 必须仍是合法 JSON，否则服务端会报结构错误而不是可用的裁剪结果。
        json.loads(fitted)

    def test_file_diffs_are_dropped_but_shape_kept(self):
        raw = json.dumps({"current": {}, "context": {"task_state": {"file_changes": [
            {"path": "a.html", "added": 3, "removed": 1, "diff": "x" * 20000}]}}})
        wire, _ = fit_wire(json.loads(raw), budget=500)
        change = wire["context"]["task_state"]["file_changes"][0]
        self.assertNotIn("diff", change)
        self.assertEqual(change["path"], "a.html")
        self.assertEqual(change["diff_chars"], 20000)

    def test_non_json_state_is_returned_unchanged(self):
        # 硬截断会把 JSON 截坏，交给调用方处理比伪造一个坏 payload 好。
        raw = "not json" * 100
        self.assertEqual(fit_state(raw)[0], raw)


class CompactTaskStateTests(unittest.TestCase):
    def test_strips_diffs_and_keeps_counters(self):
        state = {"original_goal": "画鹈鹕", "status": "active",
                 "file_changes": [{"path": "p.html", "added": 250, "removed": 0, "diff": "y" * 30000}]}
        compact = compact_task_state(state)
        self.assertEqual(compact["file_changes"][0]["diff_chars"], 30000)
        self.assertNotIn("diff", compact["file_changes"][0])
        self.assertEqual(compact["original_goal"], "画鹈鹕")

    def test_trims_tool_history_and_previews(self):
        state = {"last_tools": [{"tool": "execute_command", "ok": True,
                                 "result_preview": "z" * 5000} for _ in range(20)]}
        compact = compact_task_state(state, max_tools=6, preview=200)
        self.assertEqual(len(compact["last_tools"]), 6)
        self.assertLessEqual(len(compact["last_tools"][0]["result_preview"]), 200)

    def test_keeps_verification_fields(self):
        state = {"verification_requirements": {"mode": "static"}, "verification": {"ok": True}}
        compact = compact_task_state(state)
        self.assertEqual(compact["verification_requirements"], {"mode": "static"})
        self.assertEqual(compact["verification"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
