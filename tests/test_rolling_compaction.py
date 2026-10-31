import base64
import concurrent.futures
import copy
import json
import threading
import time
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

try:  # pillow 只在图片相关用例里用，缺失时跳过而不是整个模块导入失败
    from PIL import Image
except ImportError:  # pragma: no cover - 环境依赖
    Image = None
from test_core import Fixture, FakeJev, FakePerception
from core import config
from core.chat_compaction import ChatCompactor
from core.compaction import ContextCompactor
from core.decision_context import record_qq_message, build_decision_context
from core.harness import AgentHarness
from core.qq_policy import QQPolicy
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens


class Queue:
    def __init__(self):
        self.jobs = []

    def submit(self, session, function, *args, **kwargs):
        future = concurrent.futures.Future()
        self.jobs.append((future, function, args))
        return future

    def run(self):
        future, function, args = self.jobs.pop(0)
        future.set_result(function(*args))


class RollingCompactionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.scope = 'qq:group:88'
        self.brain = MagicMock()
        self.brain.is_configured.return_value = True
        self.brain.chat.return_value = '【摘要】旧讨论已整理，原始目标保持不变。'
        self.queue = Queue()
        self.compactor = ChatCompactor(self.memory, self.brain, self.queue)

    def fill(self, count=30, length=250):
        return [self.memory.append_chat(self.scope, {'role': 'user', 'text': str(i) + '文' * length}) for i in range(count)]

    def small_window(self):
        config.save_config({'vlm': {'context_window': 4096, 'max_tokens': 128, 'compaction_threshold_ratio': 0.6}})

    def test_character_estimate_is_point_six_and_images_are_zero(self):
        self.assertEqual(estimate_text_tokens('中文abc'), 3)
        plain = [{'role': 'user', 'content': [{'type': 'text', 'text': '参考图'}]}]
        image = copy.deepcopy(plain)
        image[0]['content'].append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'A' * 100000}})
        self.assertEqual(ContextCompactor.estimate_tokens(plain), ContextCompactor.estimate_tokens(image))
        history = [{'_id': i + 1, 'role': 'user', 'content': 'short'} for i in range(30)]
        brain = MagicMock()
        ContextCompactor(brain).update_usage(99999999, history)
        brain.chat.assert_not_called()

    def test_background_trigger_uses_system_ratio_not_fixed_message_count(self):
        self.small_window()
        self.fill(30, 110)
        config.save_config({'vlm': {'compaction_threshold_ratio': 0.95}})
        self.compactor.maybe_schedule(self.scope)
        self.assertFalse(self.queue.jobs)
        config.save_config({'vlm': {'compaction_threshold_ratio': 0.6}})
        self.compactor.maybe_schedule(self.scope)
        self.assertEqual(len(self.queue.jobs), 1)
        self.compactor.maybe_schedule(self.scope)
        self.assertEqual(len(self.queue.jobs), 1)

    def test_only_old_prefix_is_summarized_and_all_originals_remain(self):
        self.small_window()
        ids = self.fill()
        before, _ = self.memory.chat_pending(self.scope)
        self.compactor.maybe_schedule(self.scope)
        self.queue.run()
        pending, summary = self.memory.chat_pending(self.scope)
        self.assertEqual(summary['covered_until_id'], ids[-21])
        self.assertEqual(pending, before[-20:])
        original, _ = self.memory.chat_snapshot(self.scope, limit=100)
        self.assertEqual(original, before)
        source = json.loads(self.brain.chat.call_args.args[0][1]['content'])
        self.assertEqual([x['text'] for x in source['messages']], [x['text'] for x in before[:-20]])
        self.assertEqual(len(source['messages']), 10)

    def test_twenty_large_messages_are_never_compacted(self):
        self.small_window()
        self.fill(20, 5000)
        self.compactor.maybe_schedule(self.scope)
        self.assertFalse(self.queue.jobs)
        rows, _, _ = self.memory.model_chat_snapshot(self.scope)
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(len(row['text']) >= 5001 for row in rows))

    def test_new_messages_during_summary_are_not_swallowed(self):
        self.small_window()
        ids = self.fill()

        def summarize(messages):
            self.memory.append_chat(self.scope, {'role': 'user', 'text': 'NEW_AFTER_SNAPSHOT'})
            return '摘要'

        self.brain.chat.side_effect = summarize
        self.compactor.maybe_schedule(self.scope)
        self.queue.run()
        pending, summary = self.memory.chat_pending(self.scope)
        self.assertEqual(summary['covered_until_id'], ids[-21])
        self.assertEqual(len(pending), 21)
        self.assertEqual(pending[-1]['text'], 'NEW_AFTER_SNAPSHOT')

    def test_failed_summary_retains_everything_and_backoff_does_not_resubmit(self):
        self.small_window()
        self.fill()
        self.brain.chat.side_effect = OSError('offline')
        self.compactor.maybe_schedule(self.scope)
        self.queue.run()
        rows, summary = self.memory.chat_pending(self.scope)
        self.assertEqual(len(rows), 30)
        self.assertIsNone(summary)
        self.compactor.maybe_schedule(self.scope)
        self.assertFalse(self.queue.jobs)

    def test_nonreplied_chat_is_saved_in_full_and_schedules_without_reply_call(self):
        self.small_window()
        policy = QQPolicy(self.memory, None)
        policy.chat_compactor = self.compactor
        for i in range(21):
            policy.observe('完整原文' + '字' * 2000, {'message_type': 'group', 'group_id': 88, 'user_id': 123, 'message_id': i})
        self.assertEqual(len(self.queue.jobs), 1)
        self.brain.chat.assert_not_called()
        rows, _ = self.memory.chat_snapshot(self.scope)
        self.assertEqual(len(rows[0]['text']), 2004)

    def test_tool_group_boundary_keeps_at_least_twenty_original_records(self):
        history = [{'_id': i + 1, 'role': 'user', 'content': str(i)} for i in range(5)]
        history += [{'_id': 6, 'role': 'assistant', 'tool_calls': [{'id': 'a'}, {'id': 'b'}]},
                    {'_id': 7, 'role': 'tool', 'tool_call_id': 'a', 'content': 'a'},
                    {'_id': 8, 'role': 'tool', 'tool_call_id': 'b', 'content': 'b'}]
        history += [{'_id': i + 9, 'role': 'assistant', 'content': 'recent'} for i in range(19)]
        prefix = ContextCompactor.safe_prefix(history)
        self.assertEqual(len(prefix), 5)
        self.assertGreaterEqual(len(history) - len(prefix), 20)
        self.assertEqual(ContextCompactor.safe_prefix(history[-20:]), [])

    @unittest.skipIf(Image is None, "需要 pillow 才能构造测试图片")
    def test_old_images_are_refs_and_not_summary_or_history_base64(self):
        image = BytesIO()
        Image.new('RGB', (64, 96), 'red').save(image, 'PNG')
        url = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
        history = [{'_id': 1, 'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}]
        compactor = ContextCompactor(memory=self.memory, session_id='qq:private:123')
        messages = compactor.align_and_assemble_messages('system', history)
        rendered = json.dumps(messages)
        self.assertNotIn('base64', rendered)
        self.assertIn('media_ref=img_', rendered)
        self.assertEqual(history[0]['content'][0]['type'], 'image_url')

    def test_summary_commit_failure_or_cancel_does_not_advance_history(self):
        history = [self.memory.append_message('task', {'role': 'user', 'content': 'goal' + 'x' * 300}) for _ in range(30)]
        c = ContextCompactor(self.brain, self.memory, 'task')
        with patch.object(self.memory, 'save_summary', side_effect=OSError('disk')):
            c._run_compaction_worker(history)
        self.assertIsNone(c.summary)
        self.assertEqual(len(self.memory.get_history('task')[0]), 30)
        canceled = [False]

        def cancel(messages):
            canceled[0] = True
            return 'summary'

        self.brain.chat.side_effect = cancel
        c = ContextCompactor(self.brain, self.memory, 'task', cancelled=lambda: canceled[0])
        c._run_compaction_worker(history)
        self.assertIsNone(c.summary)
        self.assertEqual(len(self.memory.get_history('task')[0]), 30)

    def test_harness_compacts_mid_task_with_persistent_goal_and_no_tool_replay(self):
        config.save_config({'vlm': {'context_window': 32768, 'max_tokens': 1024}})
        path = self.root / 'source.txt'
        # 写成多行，让每一轮读到的窗口都不同：既绕开重复调用守卫，也真的累积上下文。
        path.write_text('\n'.join('evidence line %03d ' % i + 'x' * 40 for i in range(400)))
        goal = 'ORIGINAL_GOAL: 完成30次检查，保持原文件不变。'
        outer = self

        class Brain:
            steps = 0
            summaries = 0

            def chat(inner, messages):
                inner.summaries += 1
                # 摘要请求是"结构化的对话文本 + 任务检查点数据块"，不是 JSON 信封：
                # 把对话序列化成 `[用户]/[助手]/[工具结果]` 文本，模型才不会把它当成
                # 该继续的对话。原始目标由 <task_checkpoint> 单独携带，要求不可改写。
                prompt = messages[1]['content']
                outer.assertIn('<conversation>', prompt)
                outer.assertIn('<task_checkpoint>', prompt)
                outer.assertIn(goal, prompt)
                return '## 目标\n完成30次检查，保持原文件不变。\n\n## 进展\n### 已完成\n- [x] 此前检查成功\n\n## 下一步\n1. 完成剩余检查，不重做成功步骤。'

            def stream_chat_and_tools(inner, messages, tools=None):
                outer.assertIn('ORIGINAL_GOAL', json.dumps(messages))
                for index, item in enumerate(messages):
                    if item.get('role') == 'tool':
                        outer.assertTrue(any(call.get('id') == item['tool_call_id'] for earlier in messages[:index] for call in earlier.get('tool_calls', [])))
                inner.steps += 1
                if inner.steps <= 30:
                    # 每次换一个 start_line：重复调用守卫会把"同参数反复读"拦下来，
                    # 这个用例要测的是压缩，不是守卫。
                    return '', [{'id': 'read-' + str(inner.steps), 'type': 'function', 'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'file_path': str(path),
                                                 'start_line': inner.steps * 10})}}], {}
                yield 'text_delta', '检查完成。'
                return '', [], {}

        brain = Brain()
        harness = AgentHarness(brain, FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        self.assertEqual(harness.run(goal), '检查完成。')
        self.assertGreater(brain.summaries, 0)
        state = self.memory.get_task_state('local:pet')
        self.assertEqual(state['original_goal'], goal)
        self.assertEqual(state['executed_steps'], 30)
        self.assertEqual(len({x['call_id'] for x in state['last_tools']}), 30)
        # 压缩会把中间过程折叠成摘要，这里只要求历史没有被清空、且仍带着目标。
        self.assertGreaterEqual(len(self.memory.get_history('local:pet')[0]), 4)

    def test_provider_context_overflow_retries_only_after_successful_compaction(self):
        for i in range(30):
            self.memory.append_message('local:pet', {'role': 'user', 'content': 'prior' + str(i) + 'x' * 300})

        class Brain:
            calls = 0

            def chat(self, messages):
                return '旧记录摘要。当前目标仍以任务检查点为准。'

            def stream_chat_and_tools(inner, *args, **kwargs):
                inner.calls += 1
                if inner.calls == 1:
                    yield 'error', 'context_length_exceeded'
                    yield 'text_delta', 'HTTP 400 context_length_exceeded'
                    return '', [], {}
                yield 'text_delta', '继续完成。'
                return '', [], {}

        brain = Brain()
        harness = AgentHarness(brain, FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        self.assertEqual(harness.run('继续处理当前目标'), '继续完成。')
        self.assertEqual(brain.calls, 2)
        # 压缩会把中间过程折叠成摘要，这里只要求历史没有被清空、且仍带着目标。
        self.assertGreaterEqual(len(self.memory.get_history('local:pet')[0]), 4)


class CompactionTriggerTests(Fixture):
    """压缩触发方式：不能挡住这一轮，失败不能每步重试，摘要不能轮内跳变。"""

    def setUp(self):
        super().setUp()
        self.brain = MagicMock()
        self.brain.is_configured.return_value = True
        self.brain.chat.return_value = '【摘要】旧讨论已整理。'

    def _history(self, sid='local:pet', count=3, chars=20000):
        return [self.memory.append_message(sid, {'role': 'user', 'content': 'x' * chars})
                for _ in range(count)]

    def _wait_idle(self, compactor, timeout=5.0):
        deadline = time.monotonic() + timeout
        while compactor._compacting and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_soft_threshold_never_blocks_the_turn(self):
        config.save_config({'vlm': {'context_window': 32768, 'max_tokens': 1024}})
        history = self._history()
        compactor = ContextCompactor(self.brain, self.memory, 'local:pet')

        def slow(*args, **kwargs):
            time.sleep(0.5)
            return False

        with patch.object(ContextCompactor, '_run_compaction_worker', side_effect=slow):
            started = time.monotonic()
            compactor.update_usage(0, history)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.2, '撞软阈值时必须立刻返回，压缩在后台跑')
        self._wait_idle(compactor)

    def test_failed_background_compaction_backs_off(self):
        compactor = ContextCompactor(self.brain, self.memory, 'local:pet')
        with patch.object(ContextCompactor, '_run_compaction_worker', return_value=False):
            self.assertTrue(compactor.request_background([], reason='soft'))
            self._wait_idle(compactor)
            self.assertGreater(compactor._retry_after, time.monotonic(),
                               '失败后必须退避，否则每一步都会再发起一次摘要调用')
            self.assertFalse(compactor.request_background([], reason='soft'))
            self.assertEqual(compactor._failures, 1)

    def test_summary_is_frozen_within_a_turn(self):
        sid = 'local:pet'
        for i in range(4):
            self.memory.append_message(sid, {'role': 'user', 'content': 'm%d' % i})
        compactor = ContextCompactor(self.brain, self.memory, sid)
        compactor.begin_turn()
        before = compactor._current_summary()
        # 后台压完写入新摘要：本轮不能被它影响，否则前缀会在两次请求之间跳变。
        self.memory.save_summary(sid, 2, '新摘要')
        self.assertIs(compactor._current_summary(), before)
        compactor.end_turn()
        self.assertEqual(compactor._current_summary()['covered_until_id'], 2)


class MemoryConcurrencyTests(Fixture):
    def test_concurrent_read_and_write_never_hits_readonly(self):
        """两个线程一读一写：每条操作各开一个连接时，SQLite 会偶发 readonly。"""
        sid = 'local:pet'
        for i in range(5):
            self.memory.append_message(sid, {'role': 'user', 'content': 'seed%d' % i})
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    self.memory.get_history(sid)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for i in range(60):
                try:
                    self.memory.append_message(sid, {'role': 'user', 'content': 'w%d' % i})
                    self.memory.save_task_state(sid, {'i': i})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    break
        finally:
            stop.set()
            thread.join(timeout=3)
        self.assertEqual([str(e) for e in errors], [])


if __name__ == '__main__':
    unittest.main()
