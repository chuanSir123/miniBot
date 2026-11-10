import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.harness import AgentHarness
from core.process import run_bounded
from core.verification import completion_gate, source_snapshot
from test_core import Fixture, FakePerception
from core.skill_manager import SkillManager


def step(tool, arguments=None, **result):
    return {'tool': tool, 'arguments': arguments or {}, 'result': {'ok': True, **result}}


class VerificationTests(unittest.TestCase):
    def test_compile_echo_and_listing_never_count_as_tests(self):
        """命令根本不执行断言时，输出里写什么都只是文字。"""
        for command in ['Get-ChildItem', 'echo 1 passed', 'python -m py_compile app.js',
                        'python --version']:
            with self.subTest(command=command):
                trace = [step('bash', {'command': command}, exit_code=0, output='1 passed'),
                         step('capture_screen')]
                self.assertIn('功能测试缺失', completion_gate(trace, ['app.js'], 'main'))

    def test_custom_script_and_cd_prefix_are_recognised_by_their_output(self):
        """测试的认定看输出里的结算行，不看命令名像不像 pytest。

        `python tests/smoke.py`、`cd web && npx vitest`、`.\run_tests.ps1` 都是
        真实存在的调用方式；老的 runner 正则会把它们全判成"没测试"。
        """
        for command, output in [
                ('python tests/smoke.py', 'smoke ok\n3 passed in 0.21s'),
                ('cd web && npx vitest run', 'Test Files  2 passed (2)\nTests  7 passed (7)'),
                ('.\\run_tests.ps1', 'Ran 4 tests in 0.1s\nOK'),
                ('uv run pytest -q', '5 passed, 1 warning in 1.2s'),
                ('python -m unittest discover -s tests', 'All 12 tests passed')]:
            with self.subTest(command=command):
                trace = [step('bash', {'command': command}, exit_code=0, output=output)]
                self.assertEqual('', completion_gate(trace, ['app.js'], 'main'))

    def test_test_like_command_without_report_is_not_evidence(self):
        trace = [step('bash', {'command': 'python tests/smoke.py'}, exit_code=0, output='done')]
        self.assertIn('功能测试缺失', completion_gate(trace, ['app.js'], 'main'))

    def test_old_tests_failures_zero_tests_and_masked_exit_are_rejected(self):
        edit = step('edit', {'file_path': 'app.js'})
        good = step('bash', {'command': 'node --test test/app.cjs'}, exit_code=0, output='# pass 4')
        self.assertFalse(completion_gate([edit, good], ['app.js'], 'main'))
        self.assertTrue(completion_gate([good, edit], ['app.js'], 'main'))
        for result in [dict(ok=False, exit_code=1, output='1 failed'),
                       dict(exit_code=0, output='Ran 0 tests\nOK'),
                       dict(exit_code=0, output='2 passed, 1 failed'),
                       dict(exit_code=0, output='success'),
                       dict(exit_code=0, output='# pass 4', timeout=True)]:
            self.assertTrue(completion_gate([step('bash', {'command': 'python -m unittest'}, **result)], ['app.js'], 'main'))

    def test_visual_evidence_must_match_latest_artifact(self):
        """页面证据只核对"有没有渲过、有没有运行错误"，动效是否达标由放行判定看图。"""
        edit = step('write', {'file_path': 'app.html'})
        capture = step('capture_screen', target='app.html', frames_captured=4, dynamic_detected=True, runtime_checked=True)
        test = step('bash', {'command': 'node --test test_app.cjs'}, exit_code=0, output='# pass 2')
        self.assertFalse(completion_gate([edit, test, capture], ['app.html'], 'main'))
        self.assertTrue(completion_gate([capture, edit], ['app.html'], 'main'))
        self.assertTrue(completion_gate([step('capture_screen', target='other.html')], ['app.html'], 'main'))
        self.assertTrue(completion_gate([step('capture_screen')], ['app.html'], 'main'))
        failed_capture = step('capture_screen', target='app.html', ok=False, runtime_errors=['broken'])
        self.assertIn('页面核验失败', completion_gate([capture, failed_capture], ['app.html'], 'main'))
        # 文件里没有动画结构时，单帧就够；"要几帧"由文件内容决定，不由模型自报。
        self.assertEqual('', completion_gate([step('capture_screen', target='app.html', frames_captured=4,
                                                  dynamic_detected=False, runtime_checked=True)], ['app.html'], 'main'))

    def test_animated_page_needs_multiple_changing_frames(self):
        """含动画结构的页面：帧数不足或帧间零差异都由程序拦，不交给看图判断。

        这两件事查的是截图记录里的数字，不是审美：`frames=1` 拿到的单帧无论多好看
        都证明不了"在动"，连续三帧像素完全一致则说明动画根本没跑起来。
        """
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / 'bike.html'
            page.write_text(
                '<html><body><canvas id="c"></canvas><script>'
                'function loop(){ requestAnimationFrame(loop); } loop();'
                '</script></body></html>', encoding='utf-8')
            path = str(page)
            still = step('capture_screen', target=path, frames_captured=1, runtime_checked=True)
            self.assertIn('动态核验帧数不足', completion_gate([still], [path], 'main'))
            frozen = step('capture_screen', target=path, frames_captured=4, dynamic_detected=False,
                          max_diff_ratio=0.0, runtime_checked=True)
            self.assertIn('动态核验无变化', completion_gate([frozen], [path], 'main'))
            moving = step('capture_screen', target=path, frames_captured=4, dynamic_detected=True,
                          max_diff_ratio=0.03, runtime_checked=True)
            self.assertEqual('', completion_gate([moving], [path], 'main'))
            # 关掉开关（例如用户只想要一帧预览）时退回只查渲染与运行错误。
            self.assertEqual('', completion_gate([still], [path], 'main', motion_required=False))

    def test_static_page_is_not_forced_into_multi_frame_capture(self):
        """只有 hover 过渡的普通页面不该被要求多帧，否则只能靠伪造帧数过关。"""
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / 'card.html'
            page.write_text('<html><style>.b{transition:background .2s}</style>'
                            '<div class="b">hi</div></html>', encoding='utf-8')
            path = str(page)
            capture = step('capture_screen', target=path, frames_captured=1,
                           dynamic_detected=False, runtime_checked=True)
            self.assertEqual('', completion_gate([capture], [path], 'main'))

    def test_command_changes_are_tracked_and_invalidate_prior_test_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual({}, source_snapshot(root))
            path = root / 'app.js'
            path.write_text('broken', encoding='utf-8')
            self.assertIn(str(path.resolve()), source_snapshot(root))
            good = step('bash', {'command': 'node --test test_app.cjs'}, exit_code=0, output='# pass 2')
            mutate = step('bash', {'command': 'write source'}, changed_files=[str(path)])
            self.assertIn('功能测试缺失', completion_gate([good, mutate], [str(path)], 'main'))

    def test_unrelated_green_suite_cannot_hide_failed_suite(self):
        failed = step('bash', {'command': 'node --test test_a.cjs'}, ok=False, exit_code=1, output='# fail 1')
        passed = step('bash', {'command': 'node --test test_b.cjs'}, exit_code=0, output='# pass 1')
        self.assertIn('功能测试失败', completion_gate([failed, passed], ['app.js'], 'main'))
        repaired = step('bash', {'command': 'node --test test_a.cjs'}, exit_code=0, output='# pass 1')
        self.assertEqual('', completion_gate([failed, passed, repaired], ['app.js'], 'main'))

    def test_real_browser_reports_javascript_exception(self):
        from core.browser_verifier import find_browser_executable, capture_cdp_sequential_frames
        browser = find_browser_executable()
        if not browser:
            self.skipTest('Chromium is unavailable')
        try:
            import websockets
        except ImportError:
            self.skipTest('websockets is unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.html'
            path.write_text('<html><body>Looks fine<script>throw new Error("behavior is broken")</script></body></html>', encoding='utf-8')
            diagnostics = {'runtime_errors': []}
            frames, _ = capture_cdp_sequential_frames(browser, path.as_uri(), num_frames=1, diagnostics=diagnostics)
            self.assertTrue(frames)
            self.assertTrue(diagnostics.get('runtime_checked'))
            self.assertIn('behavior is broken', '\n'.join(diagnostics['runtime_errors']))

    def test_python_syntax_is_checked_without_importing_or_executing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.py'
            path.write_text('def broken(:\n', encoding='utf-8')
            self.assertIn('语法核验失败', completion_gate([], [str(path)], 'main'))

    def test_actual_broken_behavior_fails_then_fixed_behavior_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'app.py'
            source.write_text('def add(a, b): return 0\n', encoding='utf-8')
            (root / 'test_app.py').write_text('import unittest\nfrom app import add\nclass Tests(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2, 3), 5)\n', encoding='utf-8')
            def evidence():
                result = run_bounded([sys.executable, '-m', 'unittest', '-v'], cwd=directory)
                return [dict(tool='bash', arguments={'command': 'python -m unittest -v'}, result=result)]
            self.assertIn('测试失败', completion_gate(evidence(), [str(source)], 'main'))
            source.write_text('def add(a, b): return a + b\n', encoding='utf-8')
            self.assertEqual('', completion_gate(evidence(), [str(source)], 'main'))

    def test_pure_text_turn_needs_no_verification(self):
        """闲聊没有外部操作可查，不该为它做任何核验，也不该给它记成完成。"""
        harness = AgentHarness(None, None, None, None, None)
        with patch('core.config.load_config', return_value={'runtime': {'verify_completion': True}}):
            result = harness.review('你好呀', trace=[])
        self.assertEqual('unverified', result['status'])
        self.assertFalse(result['ok'])

    def test_code_change_without_evidence_is_not_delivered(self):
        """改了代码却没有任何断言结果：程序门禁打回，并说明缺哪一项。"""
        harness = AgentHarness(None, None, None, None, None)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'app.py'
            source.write_text('def add(a, b): return a + b\n', encoding='utf-8')
            with patch('core.config.load_config',
                       return_value={'runtime': {'verify_completion': True}}):
                result = harness.review(
                    '最终验收完成！亮点是动画流畅。',
                    '改一下 app.py',
                    [step('write', {'file_path': str(source)}, path=str(source))])
        self.assertFalse(result['ok'])
        self.assertIn('测试', result['feedback'])


class RepairLoopTests(Fixture):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        from core.config import load_config
        orig_cfg = load_config()
        test_cfg = dict(orig_cfg, runtime=dict(orig_cfg.get("runtime", {}), verify_completion=True))
        self._cfg_patcher = patch("core.config.load_config", return_value=test_cfg)
        self._cfg_patcher.start()

    def tearDown(self):
        self._cfg_patcher.stop()
        super().tearDown()
    def test_pass_ends_the_turn_without_feeding_anything_back(self):
        """放行即结束：通过后再往对话里塞整改意见只会让模型继续说一遍。

        只有被打回才需要回灌——回灌的目的是让它改，不是让它复述。
        """
        reviewer = MagicMock()
        reviewer.is_configured.return_value = True
        reviewer.select_fixed_action.return_value = None
        reviewer.last_error = ''
        class Brain:
            def __init__(self):
                self.calls = []
            def stream_chat_and_tools(self, messages, tools=None):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    yield 'text_delta', '先把说明改成三段式。'
                    return '', [{'id': '0', 'type': 'function',
                                 'function': {'name': 'write',
                                              'arguments': json.dumps({'file_path': 'notes.md', 'content': 'a\nb\nc'})}}], {}
                yield 'text_delta', '说明已改成三段式。'
                return '', [], {}
        brain = Brain()
        harness = AgentHarness(brain, reviewer, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        with patch.object(harness, '_dispatch_tool',
                          side_effect=lambda name, args, *extra, **kw: {'ok': True, 'path': 'notes.md'}):
            reply = harness.run('把说明改成三段式')
        self.assertEqual('说明已改成三段式。', reply)
        self.assertEqual(2, len(brain.calls), '通过后不应再发起一轮模型调用')
        self.assertEqual('complete', self.memory.get_task_state('local:pet')['status'])

    def test_pure_text_turn_closes_the_task_without_extra_model_calls(self):
        """没有外部操作的轮次：不核验、不回灌，直接收尾。

        这类轮次唯一要记下的是"没有核验过"，否则下一句会被当成续作并进旧目标，
        模型拿旧目标去对齐新要求。
        """
        reviewer = MagicMock()
        reviewer.select_fixed_action.return_value = None
        class Brain:
            def __init__(self):
                self.calls = []
            def stream_chat_and_tools(self, messages, tools=None):
                self.calls.append(messages)
                yield 'text_delta', '今天天气不错喵。'
                return '', [], {}
        brain = Brain()
        harness = AgentHarness(brain, reviewer, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        reply = harness.run('随便聊两句')
        self.assertEqual('今天天气不错喵。', reply)
        self.assertEqual(1, len(brain.calls), '闲聊不该为了核验再发起一轮模型调用')
        state = self.memory.get_task_state('local:pet')
        self.assertEqual('complete', state['status'], '没核验的轮次也要收尾')
        self.assertEqual('unverified', state['verification']['status'], '没核验就如实记成 unverified')

    def test_rejection_causes_actual_edit_retest_and_review(self):
        reviewer = MagicMock()
        reviewer.is_configured.return_value = True
        reviewer.select_fixed_action.return_value = None
        reviewer.last_error = ''
        brain = MagicMock()
        harness = AgentHarness(brain, reviewer, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        calls = []
        published = []
        progress = []
        resets = []
        def tool(name, arguments):
            return {'id': str(len(calls)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
        def stream(messages, tools=None):
            calls.append(messages)
            n = len(calls)
            if n == 1:
                yield 'text_delta', '已完成交付，亮点是全部功能。'
                return '', [tool('write', {'file_path': 'app.js', 'content': 'bad'})], {}
            if n == 2:
                yield 'text_delta', '全部完成，测试通过。'
                return '', [], {}
            if n == 3:
                # 草稿可以实时显示（界面会置灰标注作废），但还没有定稿交付。
                self.assertIn('全部完成，测试通过。', published)
                self.assertNotIn('已修复并通过测试。', published)
                self.assertIn('功能测试缺失', str(messages))
                return '', [tool('edit', {'file_path': 'app.js', 'target': 'bad', 'replacement': 'fixed'}), tool('bash', {'command': 'node --test test_app.cjs'})], {}
            yield 'text_delta', '已修复并通过测试。'
            return '', [], {}
        brain.stream_chat_and_tools.side_effect = stream
        with patch.object(harness, '_dispatch_tool', side_effect=lambda name, args, *extra, **kw: {'ok': True, **({'exit_code': 0, 'output': '# pass 2'} if name == 'bash' else {})}):
            reply = harness.run('修改 app.js 并验证功能', on_text_chunk=lambda delta, full: published.append(full), on_intermediate=progress.append, on_text_reset=resets.append)
        self.assertIn('已修复', reply)
        self.assertEqual(4, len(calls))
        self.assertEqual('complete', self.memory.get_task_state('local:pet')['status'])
        self.assertEqual(reply, published[-1])
        self.assertTrue(any('核验' in r for r in resets))
        history, _ = self.memory.get_history('local:pet')
        # 边说边做时的过程说明照常保留（不再被替换成"正在执行工具：xxx"），
        # 但被驳回的那段纯文本完成声明不会留在历史里当成交付。
        self.assertIn('已完成交付，亮点是全部功能。',
                      [m.get('content') for m in history if m['role'] == 'assistant' and m.get('tool_calls')])
        self.assertNotIn('全部完成，测试通过。',
                         [m.get('content') for m in history if m['role'] == 'assistant' and not m.get('tool_calls')])
        self.assertIn('已完成交付，亮点是全部功能。', progress)

    def test_exhaustion_never_delivers_success_claim(self):
        """测试一直是红的：重试耗尽后不许把"全部通过"当成交付。

        这里没有分类器参与，"没通过"完全由断言结算行得出，所以重试提示也是
        具体的：跑出来的失败就在那里，模型改不改是它的事，但回复不能宣称通过。
        """
        reviewer = MagicMock()
        reviewer.select_fixed_action.return_value = None
        class Brain:
            def __init__(self):
                self.n = 0
            def stream_chat_and_tools(self, *args, **kwargs):
                self.n += 1
                if self.n == 1:
                    yield 'text_delta', '先把逻辑写上。'
                    return '', [{'id': '0', 'type': 'function',
                                 'function': {'name': 'write',
                                              'arguments': json.dumps({'file_path': 'app.js', 'content': 'bad'})}}], {}
                yield 'text_delta', '所有功能完成，测试全部通过！'
                return '', [], {}
        harness = AgentHarness(Brain(), reviewer, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        published = []
        with patch.object(harness, '_dispatch_tool',
                          side_effect=lambda name, args, *e, **kw: {
                              'ok': True, 'path': 'app.js'}):
            reply = harness.run('帮我检查功能',
                                on_text_chunk=lambda delta, full: published.append(full))
        self.assertIn('未通过验证', reply)
        self.assertNotIn('所有功能完成', reply)
        self.assertEqual('failed', self.memory.get_task_state('local:pet')['verification']['status'])
        # 草稿先流式显示，但最后一帧必须是"未通过验证"的定稿。
        self.assertEqual(reply, published[-1])


if __name__ == '__main__':
    unittest.main()
