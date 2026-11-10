import threading
import time
from unittest.mock import MagicMock

from test_core import Fixture, FakePerception
from core.harness import AgentHarness
from core.skill_manager import SkillManager


class CancelTests(Fixture):
    """用户点"停止"必须立刻生效，且不能把取消占位文本写进历史。"""

    def _harness(self, brain):
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.select_fixed_action.return_value = None
        jev.final_review.return_value = {'accepted': True, 'gap': 'none', 'confidence': 0.99}
        return AgentHarness(brain, jev, self.memory, FakePerception(), SkillManager(self.root / 'skills'))

    def test_cancel_interrupts_stream_promptly_and_is_not_persisted(self):
        started = []

        class SlowBrain:
            def stream_chat_and_tools(self, messages, tools=None, cancel_event=None):
                started.append(cancel_event)
                if False:
                    yield
                # 模拟超长思考：每 0.05 秒才吐一个字符，且必须响应 cancel_event
                for i in range(200):
                    if cancel_event is not None and cancel_event.is_set():
                        return ('', [], {})
                    time.sleep(0.05)
                    yield 'text_delta', f'第{i}段'
                return ('完整回复', [], {})

        harness = self._harness(SlowBrain())
        result = {}
        done = threading.Event()

        def run():
            result['reply'] = harness.run(
                '写一个很长的任务', session_id='web:main:canceltest', scope='main', channel='web'
            )
            done.set()

        threading.Thread(target=run, daemon=True).start()
        for _ in range(100):
            if harness.is_session_active('web:main:canceltest'):
                break
            time.sleep(0.01)
        time.sleep(0.3)
        cancelled_at = time.monotonic()
        self.assertTrue(harness.cancel_session('web:main:canceltest'))
        self.assertTrue(done.wait(5), 'harness 没有在 5 秒内响应停止')
        elapsed = time.monotonic() - cancelled_at

        self.assertLess(elapsed, 2.0, f'停止耗时 {elapsed:.2f}s，仍然过慢')
        self.assertEqual(result['reply'], '任务已取消。')
        history, _ = self.memory.get_history('web:main:canceltest')
        self.assertNotIn('任务已取消。', [m.get('content') for m in history])

    def test_tool_batch_aborts_when_cancelled(self):
        calls = []

        class ToolBrain:
            def stream_chat_and_tools(self, messages, tools=None, cancel_event=None):
                if False:
                    yield
                return ('', [
                    {'id': 'c1', 'type': 'function',
                     'function': {'name': 'read', 'arguments': '{"file_path":"a.txt"}'}},
                    {'id': 'c2', 'type': 'function',
                     'function': {'name': 'read', 'arguments': '{"file_path":"b.txt"}'}},
                ], {})

        harness = self._harness(ToolBrain())
        # 第一个工具执行完就置位取消，第二个不应再执行
        original = harness._dispatch_tool

        def dispatch(name, args, *rest, **kwargs):
            calls.append(args.get('file_path'))
            harness._local.cancel.set()
            return original(name, args, *rest, **kwargs)

        harness._dispatch_tool = dispatch
        reply = harness.run('读两个文件', session_id='web:main:canceltest2', scope='main', channel='web')
        self.assertEqual(reply, '任务已取消。')
        self.assertEqual(calls, ['a.txt'])
