import json
from unittest.mock import patch

from test_core import Fixture, FakeJev, FakePerception
from core import config
from core.decision_context import build_decision_context
from core.harness import AgentHarness
from core.skill_manager import SkillManager


class SkillLifecycleTests(Fixture):
    def harness(self, brain=None):
        return AgentHarness(brain, FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))

    def test_write_file_skill_flow_discovery_disclosure_and_execution(self):
        """模拟 skill-creator 流程：write_file 写技能目录 → 自动发现 → 披露 → 执行。"""
        harness = self.harness()
        base = harness.skill_manager.skills_dir / 'echo'
        saved = harness._dispatch_tool('write_file', {
            'file_path': str(base / 'SKILL.md'),
            'content': '---\nname: echo\ndescription: 回显输入文本\nentry: scripts/echo.py\n---\n\n调用时传 prompt 参数。\n'})
        self.assertTrue(saved['ok'])
        harness._dispatch_tool('write_file', {
            'file_path': str(base / 'parameters.json'),
            'content': json.dumps({"type": "object", "properties": {
                "prompt": {"type": "string", "description": "要回显的文本"}}, "required": ["prompt"]})})
        harness._dispatch_tool('write_file', {
            'file_path': str(base / 'scripts' / 'echo.py'),
            'content': 'def execute(context):\n    return {"ok": True, "output": context.get("prompt", "hello")}\n'})
        # 自动发现（签名机制），也可显式 activate_skill 重扫
        listing = harness._dispatch_tool('list_skills', {})
        self.assertIn('echo', listing['skills'] + [s['name'] for s in listing.get('skills', [])])
        reloaded = SkillManager(self.root / 'skills')
        self.assertIn('echo', reloaded.list_skill_names())
        # 无参 → 披露；带参 → 执行
        disclosure = reloaded.execute_skill('echo')
        self.assertTrue(disclosure['ok'])
        self.assertIn('prompt', disclosure['instructions'])
        result = reloaded.execute_skill('echo', {'prompt': 'real execution'})
        self.assertTrue(result['ok'])
        self.assertIn('real execution', result['output'])

    def test_folder_skill_instructions_disclosure_and_missing_errors(self):
        manager = self.harness().skill_manager
        # 指令型技能：只有 SKILL.md，无参数调用返回完整说明
        skill_dir = manager.skills_dir / 'guide-only'
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / 'SKILL.md').write_text(
            '---\nname: guide-only\ndescription: 测试用指令型技能\n---\n\n第一步：做某事。\n',
            encoding='utf-8')
        manager.reload_skills()
        result = manager.execute_skill('guide-only')
        self.assertTrue(result['ok'])
        self.assertIn('第一步', result['instructions'])
        self.assertEqual(result['skill_dir'], str(skill_dir))
        with self.assertRaisesRegex(ValueError, '不存在'):
            manager.execute_skill('missing')
        (manager.skills_dir / 'moved.py').write_text('def execute(context): return "ok"', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已加载但未授权'):
            manager.execute_skill('moved')
        manager.activate_python_skill('moved')
        self.assertTrue(manager.execute_skill('moved')['ok'])

    def test_activate_rescans_without_importing_code(self):
        """activate_skill 新语义：重扫技能目录，绝不 import 技能代码。"""
        harness = self.harness()
        marker = self.root / 'imported.txt'
        base = harness.skill_manager.skills_dir / 'candidate'
        code = f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\ndef execute(context): return "ok"'
        harness._dispatch_tool('write_file', {'file_path': str(base / 'SKILL.md'),
            'content': '---\nname: candidate\ndescription: 候选\nentry: scripts/candidate.py\n---\n\n说明。\n'})
        harness._dispatch_tool('write_file', {'file_path': str(base / 'scripts' / 'candidate.py'), 'content': code})
        active = harness._dispatch_tool('activate_skill', {'skill_name': 'candidate'})
        self.assertTrue(active['ok'])
        self.assertIn('candidate', [s['name'] for s in active['skills']])
        self.assertFalse(marker.exists())
        listing = harness._dispatch_tool('list_skills', {})
        self.assertEqual(listing['unavailable'], [])

    def test_remote_nonowner_cannot_save_or_activate(self):
        harness = self.harness()
        config.save_config({'channels': {'napcat': {'owner_ids': ['123'], 'allow_owner_tools': True}}})
        result = harness._dispatch_tool('activate_skill', {'skill_name': 'anything'},
                                        scope='qq:private:456', session_id='qq:private:456')
        self.assertFalse(result['ok'])

    def test_turn_budget_accepts_zero_as_unlimited_and_caps_absurd_values(self):
        """0 表示不设步数上限（循环在模型不再要工具时自然结束），上限只防跑飞。"""
        config.save_config({'runtime': {'max_turns': 0}})
        self.assertEqual(config.load_config()['runtime']['max_turns'], 0)
        config.save_config({'runtime': {'max_turns': 400}})
        self.assertEqual(config.load_config()['runtime']['max_turns'], 400)
        with self.assertRaises(ValueError):
            config.save_config({'runtime': {'max_turns': 401}})

    def test_budget_checkpoint_resumes_without_replaying_successful_write(self):
        config.save_config({'runtime': {'max_turns': 1}})
        path = self.root / 'created.txt'

        class Brain:
            count = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.count += 1
                if inner.count == 1:
                    return '', [{'id': 'write-once', 'type': 'function', 'function': {
                        'name': 'write_file', 'arguments': json.dumps({'file_path': str(path), 'content': 'created'})}}], {}
                yield 'text_delta', '已经完成'
                return '已经完成', [], {}

        harness = self.harness(Brain())
        with patch.object(harness, '_dispatch_tool', wraps=harness._dispatch_tool) as dispatch:
            reply = harness.run('创建文件')
            self.assertIn('1 轮上限', reply)
            context = build_decision_context(self.memory)
            self.assertEqual(context['continuation_checkpoint']['original_goal'], '创建文件')
            self.assertTrue(context['continuation_checkpoint']['last_tools'][0]['ok'])
            self.assertEqual(harness.run('继续'), '已经完成')
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(path.read_text(encoding='utf-8'), 'created')
