import json
from unittest.mock import MagicMock, patch

from test_core import Fixture, FakePerception
from core import config
from core.compaction import ContextCompactor
from core.harness import AgentHarness
from core.jev import JevClient
from core.brain import VLMBrain
from core.skill_manager import SkillManager


class ResponseChainTests(Fixture):
    def test_changing_tail_preserves_static_and_full_history_prefix(self):
        history = [{'role': 'user', 'content': 'old question'}, {'role': 'assistant', 'content': 'answer'}]
        compactor = ContextCompactor()
        first = compactor.align_and_assemble_messages('STATIC', history, 'clock1')
        second = compactor.align_and_assemble_messages('STATIC', history + [{'role': 'user', 'content': 'new'}], 'clock2')
        self.assertEqual(first[:3], second[:3])
        # 尾块在装配层就已经落成 user：vLLM/Gemma 只接受一条前置 system，
        # 摆成 system 再指望下游改写会让每轮都重写前置 system，整段历史一起失效。
        self.assertEqual(second[-1]['role'], 'user')
        self.assertTrue(second[-1].get('_volatile_context'))
        self.assertIn('clock2', second[-1]['content'])
        wire_first = VLMBrain()._prepare_messages(first)
        wire_second = VLMBrain()._prepare_messages(second)
        self.assertEqual(wire_first[:3], wire_second[:3])
        self.assertNotIn('clock2', wire_second[0]['content'])
        self.assertEqual(wire_second[-1]['role'], 'user')
        self.assertIn('clock2', wire_second[-1]['content'])
        self.assertNotIn('_volatile_context', json.dumps(wire_second))

    def jev(self):
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.select_fixed_action.return_value = None
        jev.final_review.return_value = {'accepted': True, 'gap': 'none', 'confidence': 0.99}
        jev.last_error = ''
        return jev

    def test_chat_one_vlm_no_route_or_completion_request(self):
        jev = self.jev()
        brain = MagicMock()
        def stream(*args, **kwargs):
            yield 'text_delta', '今天辛苦了，休息一下吧。'
            return '', [], {}
        brain.stream_chat_and_tools.side_effect = stream
        harness = AgentHarness(brain, jev, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        harness.run('今天有点累，陪我聊两句')
        jev.route_request.assert_not_called()
        jev.final_review.assert_not_called()
        self.assertEqual(brain.stream_chat_and_tools.call_count, 1)

    def test_noarg_shortcut_uses_execution_gate_and_records_tool_evidence(self):
        jev = self.jev()
        jev.select_fixed_action.return_value = 'list_skills'
        class Brain:
            calls = 0
            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                result = next(m for m in messages if m['role'] == 'tool')
                self.assertTrue(json.loads(result['content'])['ok'])
                yield 'text_delta', '这是技能列表。'
                return '', [], {}
        brain = Brain()
        harness = AgentHarness(brain, jev, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        with patch.object(harness, '_dispatch_tool', wraps=harness._dispatch_tool) as dispatch:
            harness.run('请列出全部技能')
        self.assertEqual(brain.calls, 1)
        self.assertEqual(dispatch.call_args.args[:2], ('list_skills', {}))
        self.assertEqual(self.memory.get_task_state('local:pet')['executed_steps'], 1)

    def test_non_allowlisted_jev_choice_never_executes(self):
        jev = self.jev()
        jev.select_fixed_action.return_value = 'execute_command'
        class Brain:
            def stream_chat_and_tools(inner, *args, **kwargs):
                yield 'text_delta', '你好。'
                return '', [], {}
        harness = AgentHarness(Brain(), jev, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        with patch.object(harness, '_dispatch_tool') as dispatch:
            harness.run('你好')
        dispatch.assert_not_called()

    def test_selective_classifier_no_request_for_parameterized_or_chat(self):
        jev = JevClient()
        with patch.object(jev, 'decide_raw', return_value={'answers': {'noarg_tool': {'choice': 'vlm', 'confidence': 0.99}}}) as decide:
            self.assertIsNone(jev.select_fixed_action('读取文件 data/test.txt'))
            self.assertIsNone(jev.select_fixed_action('今天心情不错'))
        self.assertEqual(decide.call_count, 2)

    def test_low_confidence_or_failure_falls_back(self):
        jev = JevClient()
        with patch.object(jev, 'decide_raw', return_value={'answers': {'noarg_tool': {'choice': 'list_skills', 'confidence': .8}}}):
            self.assertIsNone(jev.select_fixed_action('列出全部技能'))
        with patch.object(jev, 'decide_raw', side_effect=TimeoutError):
            self.assertIsNone(jev.select_fixed_action('列出全部技能'))

    def test_state_puts_invariant_fields_first_for_prefix_reuse(self):
        """state 是每个 question 的共享前缀，常量必须排在前面。

        服务端把 state 拼在每个 question 之前（报错原文：the state and its media
        once, then every question），前缀从第 N 个字符分叉，后面的 KV 就全废。
        fixed_options 每次调用逐字节相同，user 必然不同，顺序反了等于从不命中。
        """
        jev = JevClient()
        with patch.object(jev, 'decide_raw', return_value={'answers': {'noarg_tool': {'choice': 'vlm', 'confidence': .99}}}) as decide:
            jev.select_fixed_action('发最后一张图', context={'recent_media': [{'media_ref': 'm1'}]})
        state = json.loads(decide.call_args.args[0])
        self.assertEqual(list(state['current']), ['fixed_options', 'user'])

    def test_steering_intent_state_puts_current_prompt_last(self):
        jev = JevClient()
        # 走 decide()，未配置 key 会在发请求前直接返回空，所以两个都要打桩。
        with patch.object(jev, 'is_configured', return_value=True), \
                patch.object(jev, 'decide_raw', return_value={'answers': {'intent_type': {'choice': 'chat'}}}) as decide:
            jev.classify_steering_intent('你好', task_state={'original_goal': '改一个登录页'})
        state = json.loads(decide.call_args.args[0])
        self.assertEqual(list(state['current']), ['current_or_last_task', 'recent_messages', 'user_input'])

    def test_fixed_arguments_are_server_owned_and_still_pass_execution_gate(self):
        jev = self.jev()
        jev.select_fixed_action.return_value = 'recent_media'
        class Brain:
            def stream_chat_and_tools(inner, *args, **kwargs):
                yield 'text_delta', '没有缓存的图片。'
                return '', [], {}
        harness = AgentHarness(Brain(), jev, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        with patch.object(harness, '_dispatch_tool', wraps=harness._dispatch_tool) as dispatch:
            harness.run('查看最近的图片')
        self.assertEqual(dispatch.call_args.args[:2], ('list_media', {'limit': 9}))

    def test_fixed_action_parameters_cannot_be_mutated_by_caller(self):
        from core.fast_actions import resolve_fixed_action
        action = resolve_fixed_action('recent_media')
        action['arguments']['limit'] = 1000
        self.assertEqual(resolve_fixed_action('recent_media')['arguments'], {'limit': 9})
        self.assertIsNone(resolve_fixed_action({'tool': 'write_file'}))

    def test_powershell_http_defaults_bypass_system_proxy(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows only')
        from test_core import FakeJev
        harness = AgentHarness(None, FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        result = harness._dispatch_tool('execute_command', {'command': "[System.Net.WebRequest]::DefaultWebProxy.IsBypassed([uri]'https://example.org')"})
        self.assertTrue(result['ok'])
        self.assertIn('True', result['output'])

    def test_late_recovery_system_does_not_move_before_history(self):
        messages = [{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'request'},
                    {'role': 'assistant', 'content': 'answer'}, {'role': 'system', 'content': 'retry instruction'}]
        wire = VLMBrain()._prepare_messages(messages)
        self.assertEqual(wire[0]['content'], 'policy')
        self.assertEqual([m['role'] for m in wire], ['system', 'user', 'assistant', 'user'])
        self.assertIn('retry instruction', wire[-1]['content'])
