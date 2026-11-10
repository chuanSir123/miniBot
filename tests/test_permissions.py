import json
import time
from unittest.mock import MagicMock

from test_core import Fixture, FakeJev, FakePerception
from core import config
from core.harness import AgentHarness, HARNESS_TOOLS
from core.qq_authority import tool_access, manage_permissions
from core.skill_manager import SkillManager


class ExecutionPermissionTests(Fixture):
    def setUp(self):
        super().setUp()
        config.save_config({'channels': {'napcat': {'owner_ids': ['123'], 'allow_owner_tools': True}}})
        self.owner = 'qq:group:88:user:123'
        self.member = 'qq:group:88:user:456'
        self.names = {t['function']['name'] for t in HARNESS_TOOLS}

    def grant(self, **args):
        return manage_permissions(self.owner, {'action': 'grant', 'user_id': '456', **args}, self.names)

    def harness(self, brain=None, jev=None):
        return AgentHarness(brain, jev or FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))

    def test_full_catalog_reaches_jev_and_vlm_but_denial_returns_to_vlm(self):
        config.save_config({'runtime': {'verify_completion': False}})
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.select_fixed_action.return_value = None
        secret = self.root / 'private.txt'
        secret.write_text('private bytes')

        class Brain:
            calls = 0
            system_seen = ''

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                inner.system_seen = inner.system_seen or json.dumps(messages[:1], ensure_ascii=False)
                # 工具名只有一套：模型拿到的就是全量工具目录本身。
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
        harness.skill_manager.register_new_skill('host_task.py', 'def execute(context): return "ok"', activate=True)
        reply = harness.run('读取文件', scope=self.member, session_id=self.member, channel='qq_group', allow_tools=False)
        self.assertIn('请主人授权', reply)
        # 技能目录挂在 system 前缀里，权限被拒也要能看到有哪些技能可用。
        self.assertIn('host_task', brain.system_seen)

    def test_group_role_never_grants_authority_only_owner_can_grant(self):
        from core.qq_authority import actor_from_event, role_from_event
        declared = {'message_type': 'group', 'group_id': 88, 'user_id': 777, 'sender': {'role': 'owner'}}
        # 只有事件声明：职位仅作展示，actor 不带后缀，权限与普通成员一致。
        self.assertEqual(role_from_event(declared), ('owner', False))
        self.assertEqual(actor_from_event(declared), 'qq:group:88:user:777')
        self.assertFalse(tool_access('qq:group:88', 'write', actor=actor_from_event(declared)))
        # 接口核验后职位进 actor（供展示），但**不带来任何权限**：群主/管理员照样要主人授权。
        admin = actor_from_event(dict(declared, _role={'role': 'owner', 'verified': True}))
        self.assertEqual(admin, 'qq:group:88:user:777@owner')
        self.assertFalse(tool_access('qq:group:88', 'write', actor=admin))
        self.assertFalse(tool_access('qq:group:88', 'bash', actor=admin))
        # 只有主人授权后才放行，且范围仍按群限定。
        manage_permissions(self.owner, {'action': 'grant', 'user_id': '777', 'tools': ['write']}, self.names)
        self.assertTrue(tool_access('qq:group:88', 'write', actor=admin))
        # 授权按群限定：换到别的群同样没有权限（会话由 actor 自带）。
        other_group = actor_from_event(dict(declared, group_id=99, _role={'role': 'owner', 'verified': True}))
        self.assertFalse(tool_access('qq:group:99', 'write', actor=other_group))
        # 群主/管理员也不能发授权：权限管理只属于主人。
        self.assertFalse(tool_access('qq:group:88', 'manage_permissions', {'action': 'grant'}, actor=admin))
        self.assertFalse(tool_access('qq:group:88', 'manage_permissions', {'action': 'revoke'}, actor=admin))
        # 主人授权可以撤销，撤销后群主也拿不到。
        manage_permissions(self.owner, {'action': 'revoke', 'user_id': '777', 'tools': ['write']}, self.names)
        self.assertFalse(tool_access('qq:group:88', 'write', actor=admin))
        # 主人身份只看 QQ 号，职位后缀不干扰。
        self.assertTrue(tool_access('qq:group:88', 'write', actor='qq:group:88:user:123@member'))

    def test_owner_natural_language_grant_persists_and_is_group_scoped(self):
        args = {'action': 'grant', 'user_id': '456', 'skills': ['image_gen'], 'duration_minutes': 30}

        class Brain:
            calls = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    return '', [{'id': 'grant', 'type': 'function', 'function': {
                        'name': 'manage_permissions', 'arguments': json.dumps(args)}}], {}
                yield 'text_delta', '已授权本群使用图片编辑30分钟。'
                return '', [], {}

        self.harness(Brain()).run('允许[@456]使用图片编辑，30分钟', scope=self.owner, session_id=self.owner, channel='qq_group')
        self.assertTrue(tool_access(self.member, 'bash'))
        self.assertFalse(tool_access('qq:private:456', 'bash'))
        self.assertFalse(tool_access('qq:group:99:user:456', 'bash'))
        self.assertFalse(tool_access(self.member, 'capture_screen'))
        config._cache = None
        config._stamp = None
        self.assertTrue(tool_access(self.member, 'bash'))

    def test_member_cannot_grant_or_query_other_members(self):
        harness = self.harness()
        result = harness._dispatch_tool('manage_permissions', {'action': 'grant', 'user_id': '456', 'tools': ['*']}, scope=self.member)
        self.assertEqual(result['error_code'], 'PERMISSION_DENIED')
        own = harness._dispatch_tool('manage_permissions', {'action': 'list'}, scope=self.member)
        self.assertTrue(own['ok'])
        other = harness._dispatch_tool('manage_permissions', {'action': 'list', 'user_id': '999'}, scope=self.member)
        self.assertFalse(other['ok'])

    def test_expiry_revocation_and_switch_are_rechecked(self):
        self.grant(skills=['image_gen'], duration_minutes=1)
        rows = config.load_config()['channels']['napcat']['permission_grants']
        rows[0]['expires_at'] = time.time() - 1
        config.save_config({'channels': {'napcat': {'permission_grants': rows}}})
        self.assertFalse(tool_access(self.member, 'bash'))
        self.grant(tools=['write'])
        harness = self.harness()
        path = self.root / 'allowed.txt'
        self.assertTrue(harness._dispatch_tool('write', {'file_path': str(path), 'content': 'ok'}, scope=self.member)['ok'])
        manage_permissions(self.owner, {'action': 'revoke', 'user_id': '456', 'tools': ['write']}, self.names)
        self.assertFalse(harness._dispatch_tool('write', {'file_path': str(path), 'content': 'bad'}, scope=self.member)['ok'])
        self.assertEqual(path.read_text(), 'ok')
        self.grant(tools=['write'])
        config.save_config({'channels': {'napcat': {'allow_owner_tools': False}}})
        self.assertFalse(tool_access(self.member, 'write'))

    def test_wildcard_grant_can_be_narrowly_revoked_without_delegating_admin(self):
        self.grant(tools=['*'])
        self.assertTrue(tool_access(self.member, 'bash'))
        self.assertFalse(tool_access(self.member, 'manage_permissions', {'action': 'grant'}))
        manage_permissions(self.owner, {'action': 'revoke', 'user_id': '456', 'tools': ['bash']}, self.names)
        self.assertFalse(tool_access(self.member, 'bash'))
        self.assertTrue(tool_access(self.member, 'write'))
        manage_permissions(self.owner, {'action': 'revoke', 'user_id': '456'}, self.names)
        self.assertFalse(tool_access(self.member, 'write'))

    def test_owner_contextual_approval_has_no_keyword_requirement(self):
        harness = self.harness()
        result = harness._dispatch_tool('manage_permissions', {'action': 'grant', 'user_id': '456', 'chat_scope': 'qq:group:88', 'skills': ['anysearch']}, scope=self.owner)
        self.assertTrue(result['ok'])
        self.assertEqual(result['chat_scope'], 'group:88')
        self.assertTrue(tool_access(self.member, 'bash'))

    def test_nonowner_quoting_owner_does_not_gain_grant_authority(self):
        harness = self.harness()
        result = harness._dispatch_tool('manage_permissions', {'action': 'grant', 'user_id': '456', 'tools': ['*']}, scope=self.member)
        self.assertEqual(result['error_code'], 'PERMISSION_DENIED')

    def test_contextual_approval_reaches_vlm_with_quoted_sender(self):
        class Brain:
            calls = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    self.assertIn('456', json.dumps(messages))
                    return '', [{'id': 'approve', 'type': 'function', 'function': {'name': 'manage_permissions',
                        'arguments': json.dumps({'action': 'grant', 'user_id': '456', 'chat_scope': 'qq:group:88', 'skills': ['anysearch']})}}], {}
                yield 'text_delta', '已为他开通本群联网搜索。'
                return '', [], {}

        self.harness(Brain()).run('[引用消息，发送者QQ=456]我能用联网搜索吗？\n可以，给他开一下',
                                  scope=self.owner, session_id=self.owner, channel='qq_group',
                                  decision_context={'direct_message': '可以，给他开一下', 'quoted_messages': [{'sender_qq': '456'}]})
        self.assertTrue(tool_access(self.member, 'bash'))

    def test_skill_grants_map_to_command_tool_and_do_not_expose_desktop_conditions(self):
        # 技能已改纯 Markdown 指令：默认成员不能执行命令，授权技能后映射到 bash。
        self.assertFalse(tool_access(self.member, 'bash'))
        self.assertTrue(tool_access(self.member, 'get_media'))
        self.assertFalse(tool_access(self.member, 'write'))
        self.grant(skills=['qq_games'])
        self.assertTrue(tool_access(self.member, 'bash'))
        self.assertFalse(tool_access(self.member, 'write'))
        result = self.harness()._dispatch_tool('schedule_reminder', {'trigger_at': 0, 'message': 'x', 'condition': 'window:secret'}, scope=self.member)
        self.assertFalse(result['ok'])

    def test_group_wide_skill_grant_and_individual_revoke(self):
        from channels.napcat_channel import NapCatChannel
        config.save_config({'channels': {'napcat': {'allowed_group_ids': ['88'], 'group_members_all': False}}})
        event = {'message_type': 'group', 'group_id': 88, 'user_id': 456}
        self.assertFalse(NapCatChannel.eligible(event))
        manage_permissions(self.owner, {'action': 'grant', 'user_id': '*', 'skills': ['image_gen']}, self.names)
        self.assertTrue(NapCatChannel.eligible(event))
        self.assertTrue(tool_access(self.member, 'bash'))
        self.assertFalse(tool_access('qq:group:99:user:456', 'bash'))
        manage_permissions(self.owner, {'action': 'revoke', 'user_id': '456', 'skills': ['image_gen']}, self.names)
        self.assertFalse(tool_access(self.member, 'bash'))
        self.assertTrue(tool_access('qq:group:88:user:789', 'bash'))
        listing = manage_permissions(self.owner, {'action': 'list'}, self.names)
        self.assertEqual(listing['user_id'], 'all')
        self.assertTrue(listing['grants'])

    def test_readonly_tools_remain_visible_but_side_effects_are_blocked(self):
        class Brain:
            calls = 0
            system_seen = ''

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                inner.system_seen = inner.system_seen or json.dumps(messages[:1], ensure_ascii=False)
                # 工具名只有一套：模型拿到的就是全量工具目录本身。
                self.assertEqual({t['function']['name'] for t in tools}, self.names)
                if inner.calls == 1:
                    return '', [{'id': 'write', 'type': 'function', 'function': {'name': 'write',
                        'arguments': json.dumps({'file_path': str(self.root / 'blocked.txt'), 'content': 'x'})}}], {}
                tool = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                self.assertEqual(tool['error_code'], 'READ_ONLY')
                yield 'text_delta', '当前只读，未修改。'
                return '', [], {}

        self.harness(Brain()).run('修改文件', readonly=True)
        self.assertFalse((self.root / 'blocked.txt').exists())


class GroupScopeTests(Fixture):
    """群聊按群号分组共享一个空间，权限按发送人QQ号各自判定。"""

    def setUp(self):
        super().setUp()
        config.save_config({'channels': {'napcat': {'owner_ids': ['123'], 'allow_owner_tools': True,
                                                    'permission_grants': []}}})
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

        # 同一个群：不分发言人，空间与会话都是群级的
        self.assertEqual((scope_a, session_a, channel_a, target_a), ('qq_group_88', 'qq:group:88', 'qq_group', '88'))
        self.assertEqual((scope_a, session_a), (scope_b, session_b))
        self.assertEqual(workspace_for(scope_a), workspace_for(scope_b))
        # 不同群互相隔离
        self.assertNotEqual(session_a, session_other)
        self.assertNotEqual(workspace_for(scope_a), workspace_for(scope_other))
        # qq_group_<群号> 与 qq:group:<群号>[:user:...] 必须落到同一个目录，不能分裂空间
        self.assertEqual(workspace_for('qq_group_88'), workspace_for('qq:group:88:user:456'))

    def test_same_group_chat_log_is_one_stream(self):
        from core.decision_context import record_qq_message

        record_qq_message(self.memory, {**self.member_a, 'message_id': '1', 'time': 1}, '甲说的话')
        record_qq_message(self.memory, {**self.member_b, 'message_id': '2', 'time': 2}, '乙说的话')
        rows, _, _ = self.memory.model_chat_snapshot('qq:group:88')
        speakers = [str((r.get('speaker') or {}).get('user_id')) for r in rows]
        self.assertIn('456', speakers)
        self.assertIn('789', speakers)
        # 别的群看不到这两条
        other_rows, _, _ = self.memory.model_chat_snapshot('qq:group:99')
        self.assertFalse(other_rows)

    def test_permissions_are_per_sender_not_per_group(self):
        from core.qq_authority import actor_from_event, tool_access, manage_permissions

        manage_permissions('qq:group:88:user:123', {'action': 'grant', 'user_id': '456',
                                                    'tools': ['write']}, self.names)
        # 同一个群空间，授权只覆盖被授权的那个QQ号
        self.assertTrue(tool_access('qq_group_88', 'write', actor=actor_from_event(self.member_a)))
        self.assertFalse(tool_access('qq_group_88', 'write', actor=actor_from_event(self.member_b)))
        # 群号不同，授权不串场
        self.assertFalse(tool_access('qq_group_99', 'write', actor=actor_from_event(self.other_group)))
        # 基础工具仍然人人可用
        self.assertTrue(tool_access('qq_group_88', 'get_media', actor=actor_from_event(self.member_b)))
