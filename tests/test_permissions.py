import json
import time
from unittest.mock import MagicMock, patch

from test_core import Fixture, FakeJev, FakePerception
from core import config
from core.harness import AgentHarness, HARNESS_TOOLS
from core.qq_authority import tool_access, manage_permissions
from core.skill_manager import SkillManager

LIKE_CMD = {
    "command": "python skills/qq-like/scripts/qq_like.py --arguments '{\"count\":1}'"
}


class ExecutionPermissionTests(Fixture):
    def setUp(self):
        super().setUp()
        # 没有 allow_owner_tools 总开关：主人由 owner_ids 决定，其余人默认不授权。
        config.save_config({'channels': {'napcat': {
            'owner_ids': ['123'], 'permission_grants': []}}})
        self.owner = 'qq:group:88:user:123'
        self.member = 'qq:group:88:user:456'
        self.names = {t['function']['name'] for t in HARNESS_TOOLS}

    def grant(self, **args):
        return manage_permissions(
            self.owner, {'action': 'grant', 'user_id': '456', **args}, self.names)

    def harness(self, brain=None, jev=None):
        return AgentHarness(
            brain, jev or FakeJev(), self.memory, FakePerception(),
            SkillManager(self.root / 'skills'))

    def test_full_catalog_reaches_jev_and_vlm_but_denial_returns_to_vlm(self):
        config.save_config({'runtime': {'verify_completion': False}})
        jev = MagicMock()
        jev.is_configured.return_value = True
        secret = self.root / 'private.txt'
        secret.write_text('private bytes')

        class Brain:
            calls = 0
            system_seen = ''

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                inner.system_seen = inner.system_seen or json.dumps(messages[:1], ensure_ascii=False)
                self.assertEqual({t['function']['name'] for t in tools}, self.names)
                if inner.calls == 1:
                    return '', [{'id': 'read', 'type': 'function', 'function': {
                        'name': 'read', 'arguments': json.dumps({'file_path': str(secret)})}}], {}
                result = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                self.assertEqual(result['error_code'], 'PERMISSION_DENIED')
                self.assertNotIn('private bytes', json.dumps(result))
                yield 'text_delta', '没有读取本机文件的权限，请主人授权。'
                return '', [], {}

        brain = Brain()
        harness = self.harness(brain, jev)
        harness.skill_manager.register_new_skill(
            'host_task.py', 'def execute(context): return "ok"', activate=True)
        reply = harness.run(
            '读取文件', scope=self.member, session_id=self.member,
            channel='qq_group', allow_tools=False)
        self.assertIn('请主人授权', reply)
        self.assertIn('host_task', brain.system_seen)

    def test_group_role_never_grants_authority_only_owner_can_grant(self):
        from core.qq_authority import actor_from_event, role_from_event

        declared = {
            'message_type': 'group', 'group_id': 88, 'user_id': 777,
            'sender': {'role': 'owner'}}
        self.assertEqual(role_from_event(declared), ('owner', False))
        self.assertEqual(actor_from_event(declared), 'qq:group:88:user:777')
        self.assertFalse(tool_access(
            'qq:group:88', 'write', actor=actor_from_event(declared)))
        admin = actor_from_event(dict(declared, _role={'role': 'owner', 'verified': True}))
        self.assertEqual(admin, 'qq:group:88:user:777@owner')
        self.assertFalse(tool_access('qq:group:88', 'write', actor=admin))
        self.assertFalse(tool_access('qq:group:88', 'bash', actor=admin))
        manage_permissions(
            self.owner, {'action': 'grant', 'user_id': '777', 'tools': ['write']}, self.names)
        self.assertTrue(tool_access('qq:group:88', 'write', actor=admin))
        other_group = actor_from_event(
            dict(declared, group_id=99, _role={'role': 'owner', 'verified': True}))
        self.assertFalse(tool_access('qq:group:99', 'write', actor=other_group))
        self.assertFalse(tool_access(
            'qq:group:88', 'manage_permissions', {'action': 'grant'}, actor=admin))
        manage_permissions(
            self.owner, {'action': 'revoke', 'user_id': '777', 'tools': ['write']}, self.names)
        self.assertFalse(tool_access('qq:group:88', 'write', actor=admin))
        self.assertTrue(tool_access(
            'qq:group:88', 'write', actor='qq:group:88:user:123@member'))

    def test_owner_natural_language_grant_is_skill_scoped(self):
        args = {
            'action': 'grant', 'user_id': '456',
            'skills': ['qq_like'], 'duration_minutes': 30}

        class Brain:
            calls = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    return '', [{'id': 'grant', 'type': 'function', 'function': {
                        'name': 'manage_permissions',
                        'arguments': json.dumps(args)}}], {}
                yield 'text_delta', '已授权本群点赞30分钟。'
                return '', [], {}

        self.harness(Brain()).run(
            '允许[@456]点赞，30分钟', scope=self.owner,
            session_id=self.owner, channel='qq_group')
        # 只授了这一个技能：能跑它的脚本，不能借此跑任意命令。
        self.assertTrue(tool_access(self.member, 'bash', LIKE_CMD))
        self.assertFalse(tool_access(self.member, 'bash', {'command': 'dir'}))
        self.assertFalse(tool_access('qq:private:456', 'bash', LIKE_CMD))
        self.assertFalse(tool_access('qq:group:99:user:456', 'bash', LIKE_CMD))
        self.assertFalse(tool_access(self.member, 'capture_screen'))
        config._cache = None
        config._stamp = None
        self.assertTrue(tool_access(self.member, 'bash', LIKE_CMD))

    def test_member_cannot_grant_or_query_other_members(self):
        harness = self.harness()
        result = harness._dispatch_tool(
            'manage_permissions',
            {'action': 'grant', 'user_id': '456', 'tools': ['*']},
            scope=self.member)
        self.assertEqual(result['error_code'], 'PERMISSION_DENIED')
        self.assertTrue(harness._dispatch_tool(
            'manage_permissions', {'action': 'list'}, scope=self.member)['ok'])
        self.assertFalse(harness._dispatch_tool(
            'manage_permissions', {'action': 'list', 'user_id': '999'},
            scope=self.member)['ok'])

    def test_expiry_and_revocation_are_rechecked(self):
        self.grant(tools=['write'], duration_minutes=1)
        rows = config.load_config()['channels']['napcat']['permission_grants']
        self.assertEqual(len(rows), 1)
        rows[0]['expires_at'] = time.time() - 1
        config.save_config({'channels': {'napcat': {'permission_grants': rows}}})
        self.assertFalse(tool_access(self.member, 'write'))
        self.grant(tools=['write'])
        harness = self.harness()
        path = self.root / 'allowed.txt'
        self.assertTrue(harness._dispatch_tool(
            'write', {'file_path': str(path), 'content': 'ok'}, scope=self.member)['ok'])
        manage_permissions(
            self.owner, {'action': 'revoke', 'user_id': '456', 'tools': ['write']}, self.names)
        self.assertFalse(harness._dispatch_tool(
            'write', {'file_path': str(path), 'content': 'bad'}, scope=self.member)['ok'])
        self.assertEqual(path.read_text(), 'ok')

    def test_wildcard_grant_can_be_narrowly_revoked(self):
        self.grant(tools=['*'])
        self.assertTrue(tool_access(self.member, 'bash'))
        self.assertFalse(tool_access(
            self.member, 'manage_permissions', {'action': 'grant'}))
        manage_permissions(
            self.owner, {'action': 'revoke', 'user_id': '456', 'tools': ['bash']}, self.names)
        self.assertFalse(tool_access(self.member, 'bash'))
        self.assertTrue(tool_access(self.member, 'write'))
        manage_permissions(
            self.owner, {'action': 'revoke', 'user_id': '456'}, self.names)
        self.assertFalse(tool_access(self.member, 'write'))

    def test_owner_contextual_approval_has_no_keyword_requirement(self):
        result = self.harness()._dispatch_tool(
            'manage_permissions',
            {'action': 'grant', 'user_id': '456',
             'chat_scope': 'qq:group:88', 'skills': ['image-gen']},
            scope=self.owner)
        self.assertTrue(result['ok'])
        self.assertEqual(result['chat_scope'], 'group:88')
        self.assertEqual(result['granted'], [{'kind': 'skill', 'name': 'image_gen'}])

    def test_nonowner_quoting_owner_does_not_gain_grant_authority(self):
        result = self.harness()._dispatch_tool(
            'manage_permissions',
            {'action': 'grant', 'user_id': '456', 'tools': ['*']},
            scope=self.member)
        self.assertEqual(result['error_code'], 'PERMISSION_DENIED')

    def test_contextual_approval_reaches_vlm_with_quoted_sender(self):
        class Brain:
            calls = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    self.assertIn('456', json.dumps(messages))
                    return '', [{'id': 'approve', 'type': 'function', 'function': {
                        'name': 'manage_permissions',
                        'arguments': json.dumps({
                            'action': 'grant', 'user_id': '456',
                            'chat_scope': 'qq:group:88', 'tools': ['write']})}}], {}
                yield 'text_delta', '已为他开通本群写文件。'
                return '', [], {}

        self.harness(Brain()).run(
            '[引用消息，发送者QQ=456]我能写文件吗？\n可以，给他开一下',
            scope=self.owner, session_id=self.owner, channel='qq_group',
            decision_context={
                'direct_message': '可以，给他开一下',
                'quoted_messages': [{'sender_qq': '456'}]})
        self.assertTrue(tool_access(self.member, 'write'))

    def test_skill_grant_does_not_expose_arbitrary_commands(self):
        self.assertFalse(tool_access(self.member, 'bash'))
        self.assertTrue(tool_access(self.member, 'get_media'))
        self.assertFalse(tool_access(self.member, 'write'))
        self.grant(skills=['qq_games'])
        self.assertTrue(tool_access(
            self.member, 'bash',
            {'command': 'python skills/qq_games/scripts/qq_games.py'}))
        self.assertFalse(tool_access(self.member, 'bash', {'command': 'dir'}))
        self.assertFalse(tool_access(self.member, 'write'))
        result = self.harness()._dispatch_tool(
            'schedule_reminder',
            {'trigger_at': 0, 'message': 'x', 'condition': 'window:secret'},
            scope=self.member)
        self.assertFalse(result['ok'])

    def test_global_grant_to_everyone_and_individual_revoke(self):
        from channels.napcat_channel import NapCatChannel

        config.save_config({'channels': {'napcat': {
            'allowed_group_ids': ['88'], 'group_members_all': False}}})
        event = {'message_type': 'group', 'group_id': 88, 'user_id': 456}
        self.assertFalse(NapCatChannel.eligible(event))
        result = manage_permissions(
            self.owner,
            {'action': 'grant', 'user_id': '*', 'chat_scope': 'all',
             'skills': ['qq_like']}, self.names)
        self.assertTrue(result['ok'])
        self.assertTrue(NapCatChannel.eligible(event))
        self.assertTrue(tool_access(self.member, 'bash', LIKE_CMD))
        self.assertTrue(tool_access('qq:group:99:user:456', 'bash', LIKE_CMD))
        self.assertFalse(tool_access(self.member, 'bash', {'command': 'dir'}))
        manage_permissions(
            self.owner,
            {'action': 'revoke', 'user_id': '456', 'skills': ['qq_like']}, self.names)
        self.assertFalse(tool_access(self.member, 'bash', LIKE_CMD))
        self.assertFalse(NapCatChannel.eligible(event))
        self.assertTrue(tool_access('qq:group:88:user:789', 'bash', LIKE_CMD))

    def test_readonly_tools_remain_visible_but_side_effects_are_blocked(self):
        class Brain:
            calls = 0
            system_seen = ''

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                inner.system_seen = inner.system_seen or json.dumps(messages[:1], ensure_ascii=False)
                self.assertEqual({t['function']['name'] for t in tools}, self.names)
                if inner.calls == 1:
                    return '', [{'id': 'write', 'type': 'function', 'function': {
                        'name': 'write', 'arguments': json.dumps({
                            'file_path': str(self.root / 'blocked.txt'),
                            'content': 'x'})}}], {}
                tool = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                self.assertEqual(tool['error_code'], 'READ_ONLY')
                yield 'text_delta', '当前只读，未修改。'
                return '', [], {}

        self.harness(Brain()).run('修改文件', readonly=True)
        self.assertFalse((self.root / 'blocked.txt').exists())


class SkillSubjectTests(Fixture):
    """技能脚本的执行期识别、文档读取特例与两阶段确认。"""

    def setUp(self):
        super().setUp()
        config.save_config({'channels': {'napcat': {
            'owner_ids': ['123'], 'permission_grants': []}}})
        self.owner = 'qq:group:88:user:123'
        self.member = 'qq:group:88:user:456'
        self.names = {t['function']['name'] for t in HARNESS_TOOLS}

    def test_command_recognition_rejects_injection_and_accepts_dir_alias(self):
        from core.permission_deps import normalize_skill, skill_from_command

        self.assertEqual(normalize_skill('image-gen'), 'image_gen')
        self.assertEqual(normalize_skill('image_gen'), 'image_gen')
        self.assertEqual(normalize_skill('nope'), '')
        self.assertEqual(skill_from_command(LIKE_CMD['command']), 'qq_like')
        self.assertEqual(skill_from_command(
            'python skills/image-gen/scripts/image_gen.py'), 'image_gen')
        for bad in (
                'python skills/qq-like/scripts/qq_like.py; whoami',
                'python skills/qq-like/scripts/qq_like.py && whoami',
                'python skills/qq-like/scripts/missing.py',
                'python C:/skills/qq-like/scripts/qq_like.py',
                'dir'):
            self.assertEqual(skill_from_command(bad), '', bad)
        self.assertEqual(skill_from_command(
            "python skills/qq-like/scripts/qq_like.py --arguments '{\"a\":\"b&c\"}'"),
            'qq_like')

    def test_skill_doc_read_is_public_but_arbitrary_file_read_is_not(self):
        self.assertTrue(tool_access(
            self.member, 'read', {'file_path': 'skills/qq-like/SKILL.md'}))
        self.assertFalse(tool_access(
            self.member, 'read', {'file_path': str(self.root / 'secret.txt')}))
        self.assertFalse(tool_access(
            self.member, 'read', {'file_path': '../../secret.md'}))

    def test_dependencies_require_confirmation_and_never_auto_grant(self):
        deps = [{'kind': 'tool', 'name': 'write', 'reason': '测试依赖'}]
        args = {
            'action': 'grant', 'user_id': '*', 'chat_scope': 'all',
            'skills': ['qq_like']}
        with patch('core.qq_authority._dependencies', return_value=deps):
            pending = manage_permissions('qq_group_88', args, self.names, actor=self.owner)
            self.assertTrue(pending['needs_confirmation'])
            self.assertEqual(pending['dependencies'], deps)
            self.assertEqual(
                config.load_config()['channels']['napcat']['permission_grants'], [])
            confirmed = manage_permissions(
                'qq_group_88',
                {**args, 'confirm': True, 'confirm_token': pending['confirm_token']},
                self.names, actor=self.owner)
            self.assertTrue(confirmed['ok'])
            self.assertEqual(
                confirmed['granted'], [{'kind': 'skill', 'name': 'qq_like'}])
            rows = config.load_config()['channels']['napcat']['permission_grants']
            self.assertEqual([r['name'] for r in rows], ['qq_like'])
            self.assertFalse(tool_access(self.member, 'write'))

    def test_confirm_token_is_single_use_and_actor_bound(self):
        deps = [{'kind': 'tool', 'name': 'write', 'reason': '测试依赖'}]
        args = {'action': 'grant', 'user_id': '456', 'tools': ['read']}
        with patch('core.qq_authority._dependencies', return_value=deps):
            pending = manage_permissions('qq_group_88', args, self.names, actor=self.owner)
            retry = {**args, 'confirm': True,
                     'confirm_token': pending['confirm_token']}
            with self.assertRaises(ValueError):
                manage_permissions('qq_group_88', retry, self.names, actor=self.member)
            self.assertTrue(manage_permissions(
                'qq_group_88', retry, self.names, actor=self.owner)['ok'])
            with self.assertRaises(ValueError):
                manage_permissions('qq_group_88', retry, self.names, actor=self.owner)

    def test_everyone_can_list_grants(self):
        manage_permissions(
            'qq_group_88',
            {'action': 'grant', 'user_id': '*', 'chat_scope': 'all',
             'skills': ['qq_like']}, self.names, actor=self.owner)
        rows = manage_permissions(
            'qq_group_88', {'action': 'list'}, self.names, actor=self.member)
        self.assertTrue(rows['ok'])
        self.assertTrue(any(r['user_id'] == '*' for r in rows['grants']))


class GroupScopeTests(Fixture):
    """群聊按群号分组共享一个空间，权限按发送人QQ号各自判定。"""

    def setUp(self):
        super().setUp()
        config.save_config({'channels': {'napcat': {
            'owner_ids': ['123'], 'permission_grants': []}}})
        self.member_a = {'message_type': 'group', 'group_id': 88, 'user_id': 456}
        self.member_b = {'message_type': 'group', 'group_id': 88, 'user_id': 789}
        self.other_group = {'message_type': 'group', 'group_id': 99, 'user_id': 456}
        self.names = {t['function']['name'] for t in HARNESS_TOOLS}

    def test_group_chat_shares_one_scope_and_workspace(self):
        from main import MiniBotApp
        from core.workspace import workspace_for

        scope_a, session_a, channel_a, target_a, _ = MiniBotApp.identity(self.member_a)
        scope_b, session_b, _, _, _ = MiniBotApp.identity(self.member_b)
        scope_other, session_other, _, _, _ = MiniBotApp.identity(self.other_group)
        self.assertEqual(
            (scope_a, session_a, channel_a, target_a),
            ('qq_group_88', 'qq:group:88', 'qq_group', '88'))
        self.assertEqual((scope_a, session_a), (scope_b, session_b))
        self.assertEqual(workspace_for(scope_a), workspace_for(scope_b))
        self.assertNotEqual(session_a, session_other)
        self.assertNotEqual(workspace_for(scope_a), workspace_for(scope_other))
        self.assertEqual(
            workspace_for('qq_group_88'), workspace_for('qq:group:88:user:456'))

    def test_same_group_chat_log_is_one_stream(self):
        from core.decision_context import record_qq_message

        record_qq_message(self.memory, {**self.member_a, 'message_id': '1', 'time': 1}, '甲说的话')
        record_qq_message(self.memory, {**self.member_b, 'message_id': '2', 'time': 2}, '乙说的话')
        rows, _, _ = self.memory.model_chat_snapshot('qq:group:88')
        speakers = [str((r.get('speaker') or {}).get('user_id')) for r in rows]
        self.assertIn('456', speakers)
        self.assertIn('789', speakers)
        other_rows, _, _ = self.memory.model_chat_snapshot('qq:group:99')
        self.assertFalse(other_rows)

    def test_permissions_are_per_sender_not_per_group(self):
        from core.qq_authority import actor_from_event, tool_access

        manage_permissions(
            'qq:group:88:user:123',
            {'action': 'grant', 'user_id': '456', 'tools': ['write']}, self.names)
        self.assertTrue(tool_access(
            'qq_group_88', 'write', actor=actor_from_event(self.member_a)))
        self.assertFalse(tool_access(
            'qq_group_88', 'write', actor=actor_from_event(self.member_b)))
        self.assertFalse(tool_access(
            'qq_group_99', 'write', actor=actor_from_event(self.other_group)))
        self.assertTrue(tool_access(
            'qq_group_88', 'get_media', actor=actor_from_event(self.member_b)))
