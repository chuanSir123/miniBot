import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core.memory import MiniMemory
from core.scheduling import next_trigger

class SchedulingTests(unittest.TestCase):
    def test_interval_skips_missed_runs(self):
        self.assertEqual(next_trigger('interval:60', 100, 285), 340)
        with self.assertRaises(ValueError):
            next_trigger('interval:0', 100, 100)

    def test_recurring_task_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'memory.db'
            memory = MiniMemory(path)
            task = memory.add_delayed_intent(100, 'hello', repeat_rule='interval:60')
            with patch('core.memory.time.time', return_value=285):
                self.assertEqual(memory.claim_due_intents()[0]['id'], task)
                memory.finish_intent(task, True)
            row = MiniMemory(path).list_intents()[0]
            self.assertEqual((row['status'], row['trigger_at'], row['attempts']), ('pending', 340, 0))

    def test_one_shot_stays_delivered(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = MiniMemory(Path(folder) / 'memory.db')
            task = memory.add_delayed_intent(1, 'hello')
            memory.claim_due_intents()
            memory.finish_intent(task, True)
            self.assertEqual(memory.list_intents()[0]['status'], 'delivered')

    def test_calendar_first_run_uses_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = MiniMemory(Path(folder) / 'memory.db')
            with patch('core.memory.time.time', return_value=1793459400):
                memory.add_delayed_intent(1, 'hello', repeat_rule='daily 08:00')
            import datetime
            row = memory.list_intents()[0]
            local = datetime.datetime.fromtimestamp(row['trigger_at'])
            self.assertEqual((local.hour, local.minute), (8, 0))
            self.assertGreater(row['trigger_at'], 1793459400)
