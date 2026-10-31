"""Unit tests for Background Task Manager, timeout handling, and sync/async progressive waiting."""

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.harness import AgentHarness
from core.memory import MiniMemory as Memory
from core.skill_manager import SkillManager
from core.task_manager import TaskManager


class FakeBrain:
    pass


class FakeJev:
    def decide_and_plan(self, *args, **kwargs):
        return {"action": "reply", "thought": "ok"}


class FakePerception:
    def detect_active_window(self):
        return {"title": "Desktop"}


class TestTaskManagerAndHarness(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.skills_dir = self.root / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.memory = Memory(self.root / "test.db")
        self.skill_manager = SkillManager(self.skills_dir)
        self.harness = AgentHarness(
            FakeBrain(),
            FakeJev(),
            self.memory,
            FakePerception(),
            self.skill_manager,
        )
        self.tm = TaskManager.get_instance()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_task_manager_quick_execution(self):
        cmd = [sys.executable, "-c", "import sys; print('hello task'); sys.exit(0)"]
        task = self.tm.start_task(cmd, name="quick_test")
        res = self.tm.wait_task(task.task_id, timeout_sec=3.0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("hello task", res["output"])

    def test_task_manager_timeout_no_kill_then_sync_wait(self):
        # Long command: prints step 1, sleeps, prints step 2
        code = "import time; print('step 1', flush=True); time.sleep(0.8); print('step 2', flush=True)"
        cmd = [sys.executable, "-c", code]
        task = self.tm.start_task(cmd, name="sleep_test")

        # Step 1: Initial wait times out (0.2s) - must NOT kill the process
        res1 = self.tm.wait_task(task.task_id, timeout_sec=0.2)
        self.assertTrue(res1["ok"])
        self.assertTrue(res1.get("still_running"))
        self.assertEqual(res1["status"], "running")
        self.assertTrue(task.is_alive())
        self.assertIn("step 1", res1["output"])

        # Step 2: Sync wait continues until completion
        res2 = self.tm.wait_task(task.task_id, timeout_sec=2.0)
        self.assertTrue(res2["ok"])
        self.assertFalse(res2.get("still_running", False))
        self.assertEqual(res2["status"], "completed")
        self.assertIn("step 2", res2["output"])

    def test_task_manager_async_polling_check(self):
        code = "import time; print('working', flush=True); time.sleep(0.4); print('finished', flush=True)"
        cmd = [sys.executable, "-c", code]
        task = self.tm.start_task(cmd, name="poll_test")

        chk1 = self.tm.check_task(task.task_id)
        self.assertTrue(chk1["ok"])
        self.assertIn(chk1["status"], ("running", "completed"))

        # Wait until it finishes
        task.wait(timeout=2.0)
        chk2 = self.tm.check_task(task.task_id)
        self.assertTrue(chk2["ok"])
        self.assertEqual(chk2["status"], "completed")
        self.assertEqual(chk2["exit_code"], 0)
        self.assertIn("finished", chk2["tail_output"])

    def test_task_manager_cancellation(self):
        code = "import time; print('alive', flush=True); time.sleep(10)"
        cmd = [sys.executable, "-c", code]
        task = self.tm.start_task(cmd, name="cancel_test")
        time.sleep(0.2)
        self.assertTrue(task.is_alive())

        res = self.tm.cancel_task(task.task_id)
        self.assertTrue(res["ok"])
        self.assertEqual(task.status, "cancelled")
        time.sleep(0.3)
        self.assertFalse(task.is_alive())

    def test_harness_tools_dispatch(self):
        # 1. execute_command with background=True
        res = self.harness._dispatch_tool(
            "execute_command",
            {"command": "Write-Output 'bg output'", "background": True},
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "running")
        task_id = res["task_id"]

        # 2. check_task
        chk = self.harness._dispatch_tool("check_task", {"task_id": task_id})
        self.assertTrue(chk["ok"])

        # 3. wait_task
        wait_res = self.harness._dispatch_tool(
            "wait_task", {"task_id": task_id, "timeout_sec": 3}
        )
        self.assertTrue(wait_res["ok"])
        self.assertEqual(wait_res["status"], "completed")
        self.assertIn("bg output", wait_res["output"])

        # 4. cancel_task
        cancel_res = self.harness._dispatch_tool(
            "cancel_task", {"task_id": task_id}
        )
        self.assertTrue(cancel_res["ok"])

    def test_skill_timeout_and_progressive_wait(self):
        # Create a Python skill that runs for 0.8 seconds
        skill_code = """
import time
def execute(context):
    print("skill step 1", flush=True)
    time.sleep(0.8)
    return {"ok": True, "output": "skill done"}
"""
        self.skill_manager.register_new_skill(
            "sleeper.py",
            skill_code,
            description="A skill that sleeps",
            activate=True,
        )

        # Call skill with short timeout (0.2s) -> should NOT kill, should return still_running
        res1 = self.harness._dispatch_tool(
            "execute_skill",
            {"skill_name": "sleeper", "timeout_sec": 0.2},
        )
        self.assertTrue(res1["ok"])
        self.assertTrue(res1.get("still_running"))
        task_id = res1["task_id"]

        # Now sync wait for the task to finish
        res2 = self.harness._dispatch_tool(
            "wait_task",
            {"task_id": task_id, "timeout_sec": 2.0},
        )
        self.assertTrue(res2["ok"])
        self.assertFalse(res2.get("still_running", False))
        self.assertEqual(res2.get("output"), "skill done")


if __name__ == "__main__":
    unittest.main()
