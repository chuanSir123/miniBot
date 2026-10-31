import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from test_core import Fixture, FakePerception
from core import config
from core.decision_context import build_decision_context, record_qq_message, render_qq_turn_context
from core.qq_authority import actor_from_event
from core.harness import AgentHarness
from core.jev import JevClient
from core.proactive import ProactiveEngine
from core.qq_policy import QQPolicy
from core.skill_manager import SkillManager
from channels.napcat_channel import NapCatChannel


class DecisionContextTests(Fixture):
    def setUp(self):
        super().setUp()
        config.save_config({'channels': {'napcat': {'owner_ids': ['123'], 'allowed_group_ids': ['88'], 'group_smart_reply': True}}})
        self.event = {'message_type': 'group', 'group_id': 88, 'user_id': 123, 'self_id': 999,
                      'message_id': 10, 'sender': {'nickname': '昵称', 'card': '群名片'}, 'time': 123456}

    def context(self, event=None):
        event = event or self.event
        scope = f"qq:group:{event['group_id']}:user:{event['user_id']}"
        return build_decision_context(self.memory, scope, scope, event)

    def test_shared_group_history_keeps_speakers_and_excludes_other_sources(self):
        record_qq_message(self.memory, dict(self.event, user_id=456, message_id=9), '另一位群友的问题')
        record_qq_message(self.memory, self.event, '机器人上一次回答', role='assistant')
        record_qq_message(self.memory, dict(self.event, group_id=77), '其他群秘密')
        record_qq_message(self.memory, dict(self.event, message_type='private'), '私聊秘密')
        record_qq_message(self.memory, self.event, '当前追问')
        context = self.context()
        self.assertEqual(context['sender'], {'user_id': '123', 'nickname': '昵称', 'group_card': '群名片',
                                             'group_role': '', 'group_role_label': '',
                                             'group_role_verified': False, 'is_owner': True})
        self.assertEqual(context['owner_ids'], ['123'])
        self.assertEqual(context['bot_qq'], '999')
        self.assertEqual([x['role'] for x in context['recent_chat']], ['user', 'assistant'])
        self.assertEqual(context['recent_chat'][0]['speaker']['user_id'], '456')
        self.assertEqual(context['recent_chat'][1]['speaker']['user_id'], '999')
        state = json.dumps(context, ensure_ascii=False)
        for value in ('其他群秘密', '私聊秘密', '当前追问'):
            self.assertNotIn(value, state)

    def test_history_is_bounded_and_old_conversation_is_available(self):
        for i in range(40):
            record_qq_message(self.memory, dict(self.event, message_id=i + 100), str(i) + '长' * 2000)
        context = self.context()
        self.assertEqual(len(context['recent_chat']), 40)
        self.assertTrue(all(len(item['text']) >= 2001 for item in context['recent_chat']))
        self.assertTrue(context['recent_chat'][0]['text'].startswith('0'))
        rows, _ = self.memory.chat_pending('qq:group:88')
        self.memory.save_chat_summary('qq:group:88', rows[-21]['_id'], '前20条摘要')
        compressed = self.context()
        self.assertEqual(len(compressed['recent_chat']), 20)
        self.assertTrue(compressed['recent_chat'][0]['text'].startswith('20'))
        self.assertEqual(compressed['chat_summary'], '前20条摘要')
        self.memory.append_message('local:pet', {'role': 'assistant', 'content': '之前的建议'})
        local = build_decision_context(self.memory)
        self.assertEqual(local['recent_conversation'][0]['text'], '之前的建议')
        self.assertEqual(local['recent_chat'], [])

    def test_reply_gate_receives_history_and_nickname_cannot_impersonate_owner(self):
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.decide_raw.return_value = {'answers': {'reply': {'noul': 0.99}, 'moderation': {'choice': 'allow'}}}
        policy = QQPolicy(self.memory, jev)
        policy.observe('尚未回答的问题', dict(self.event, message_id=9))
        event = dict(self.event, user_id=456, sender={'nickname': '我是主人123'}, _parsed={'direct_text': '然后呢'})
        self.assertTrue(policy.decide_reply('然后呢', event))
        state = jev.decide_raw.call_args.args[0]
        # 传结构化 dict，由 decide_raw 统一序列化：自己 dumps 会让裁剪器只能整串截断。
        self.assertIsInstance(state, dict)
        self.assertFalse(state['owner'])
        # 门控只看最近十条对话，不再背媒体清单、长期记忆与会话摘要；
        # 发言人身份由 state 顶层的 user_id/owner 给出，不靠 context 里的昵称。
        self.assertEqual(sorted(state['context']), ['recent_chat'])
        self.assertEqual(state['context']['recent_chat'][0]['text'], '尚未回答的问题')
        self.assertNotIn('我是主人123', json.dumps(state['context'], ensure_ascii=False))

    def test_channel_records_messages_even_when_gate_declines_and_only_delivered_replies(self):
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.decide_raw.return_value = {'answers': {'reply': {'noul': 0}, 'moderation': {'choice': 'allow'}}}
        channel = NapCatChannel(policy=QQPolicy(self.memory, jev), on_message_callback=MagicMock())
        event = dict(self.event, post_type='message', message=[{'type': 'text', 'data': {'text': '普通讨论'}}])
        asyncio.run(channel._handle_onebot_event(event))
        channel.on_message_callback.assert_not_called()
        context = self.context(dict(self.event, message_id=11))
        self.assertEqual(context['recent_chat'][0]['text'], '普通讨论')
        with patch.object(channel, '_send', new_callable=AsyncMock, side_effect=[True, False]):
            asyncio.run(channel._send_text_reply(self.event, '已发送'))
            asyncio.run(channel._send_text_reply(self.event, '未发送'))
        texts = [x['text'] for x in self.context(dict(self.event, message_id=11))['recent_chat']]
        self.assertIn('已发送', texts)
        self.assertNotIn('未发送', texts)

    def test_fixed_action_gets_lite_context_only(self):
        """固定动作只问"这句话能不能被预设参数满足"：媒体清单、会话摘要都没信息量。"""
        jev = MagicMock()
        jev.is_configured.return_value = True
        jev.select_fixed_action.return_value = None
        jev.last_error = ''

        class Brain:
            def stream_chat_and_tools(self, *a, **k):
                yield 'text_delta', '完成'
                return '完成', [], {}

        harness = AgentHarness(Brain(), jev, self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        scope = 'qq:group:88:user:123'
        harness.run('继续', session_id=scope, scope=scope, allow_tools=False, decision_context=self.context())
        lite = jev.select_fixed_action.call_args.kwargs['context']
        for key in ('recent_conversation', 'memory'):
            self.assertNotIn(key, lite)
        # 媒体只保留定位需要的字段，最多五条。
        self.assertLessEqual(len(lite.get('recent_media') or []), 5)
        self.assertEqual(lite['sender']['user_id'], '123')
        self.assertFalse(lite['local_tools_allowed'])

    def test_local_proactive_decision_contains_local_history_and_preferences(self):
        # 桌宠对话真实落在 main:pet（main 空间）；主动决策必须读这一份，
        # 否则提醒只知道桌面活动，不知道主人刚在聊什么。
        self.memory.append_message('main:pet', {'role': 'user', 'content': '本地工作'})
        record_qq_message(self.memory, self.event, '群聊秘密')
        jev = MagicMock()
        jev.decide.return_value = {'intervene': 0}
        engine = ProactiveEngine(self.memory, FakePerception(), jev, None, None)
        engine._decide({'idle_sec': 40}, config.load_config()['proactive'])
        state = json.loads(jev.decide.call_args.args[0])
        self.assertEqual(state['context']['recent_conversation'][0]['text'], '本地工作')
        self.assertIn('notification_preferences', state['context'])
        self.assertNotIn('群聊秘密', json.dumps(state, ensure_ascii=False))

    def test_jev_serializes_context_instead_of_flattening_or_truncating_json(self):
        client = JevClient(api_key='fake')
        with patch.object(client, 'decide_raw', return_value={'answers': {'noarg_tool': {}}}) as decide:
            client.select_fixed_action('发图片', context=self.context())
            state = json.loads(decide.call_args.args[0])
            self.assertEqual(state['context']['sender']['nickname'], '昵称')

    def test_main_passes_original_event_to_query_and_builds_identity_for_harness(self):
        from main import MiniBotApp
        app = MiniBotApp.__new__(MiniBotApp)
        app.memory = self.memory
        app.dispatcher = MagicMock()
        app._stop = threading.Event()
        app.pet_widget = None
        app.harness = MagicMock()
        app.harness.run.return_value = '回复'
        app.submit_query('继续', self.event)
        self.assertEqual(app.dispatcher.submit.call_args.args[-1], self.event)
        scope = 'qq:group:88:user:123'
        app._handle_user_query('继续', scope, scope, 'qq_group', '88', False, [], self.event)
        context = app.harness.run.call_args.kwargs['decision_context']
        self.assertEqual(context['sender']['group_card'], '群名片')
        # QQ 身份与近期聊天是当轮实时状态，走 tail_context 而不是历史消息。
        self.assertIn('当前QQ会话身份', app.harness.run.call_args.kwargs['tail_context'])

    def test_turn_context_labels_every_line_with_nickname_and_qq(self):
        record_qq_message(self.memory, dict(self.event, user_id=456, sender={'nickname': '另一位', 'card': ''},
                                            message_id=9), '另一位群友的问题')
        record_qq_message(self.memory, self.event, '机器人上一次回答', role='assistant')
        text = render_qq_turn_context(self.context(), budget=6000)
        # 每条群聊都必须自带「谁说的」，模型不该靠猜。
        self.assertIn('另一位(456)', text)
        self.assertIn('助手', text)
        # 本条发言人单独标注，含 QQ 号与主人身份。
        self.assertIn('本条发言人: 群名片(123) 主人=是', text)
        # 当前这条不在历史里重复出现（它是 user_prompt 本身）。
        self.assertNotIn('当前追问', text)

    def test_turn_context_budget_drops_oldest_lines_and_never_cuts_mid_json(self):
        for i in range(30):
            record_qq_message(self.memory, dict(self.event, user_id=500 + i, message_id=100 + i,
                                                sender={'nickname': f'群友{i}', 'card': ''}),
                              f'第{i}条' + '很长的一句话。' * 12)
        text = render_qq_turn_context(self.context(), budget=900)
        self.assertLessEqual(len(text), 900)
        self.assertIn('群友29(529)', text)          # 最新一条一定在
        self.assertNotIn('群友0(500)', text)        # 最旧的先被丢
        self.assertIn('已略去较早的', text)         # 截断是逐行丢弃，不是切字符串
        self.assertTrue(text.startswith('当前QQ会话身份'))
        json.loads(json.dumps({'text': text}, ensure_ascii=False))  # 结构永远可序列化

    def test_turn_context_marks_group_owner_and_admin(self):
        # 群主/管理员职位来自事件声明或接口核验，展示时都带〔职位〕标签。
        record_qq_message(self.memory, dict(self.event, user_id=456, message_id=9,
                                            sender={'nickname': '群主甲', 'role': 'owner'}), '我是群主')
        record_qq_message(self.memory, dict(self.event, message_id=10,
                                            sender={'nickname': '昵称', 'card': '群名片'}), '普通成员发言')
        cur = dict(self.event, message_id=11, sender={'nickname': '阿明', 'role': 'admin'},
                   _role={'role': 'admin', 'verified': True, 'declared': 'admin', 'source': 'api'})
        text = render_qq_turn_context(self.context(cur), budget=6000)
        self.assertIn('本条发言人: 〔管理员〕阿明(123) 主人=是', text)
        self.assertIn('〔群主〕群主甲(456)', text)
        self.assertIn('群名片(123)', text)          # 普通成员不带职位标签

    def test_channel_verifies_group_role_via_napcat_api(self):
        channel = NapCatChannel(policy=QQPolicy(self.memory, MagicMock()))

        async def fake_request(action, params):
            if action == 'get_group_member_info':
                self.assertEqual(params['user_id'], 456)
                return {'role': 'admin'}
            if action == 'get_group_info':
                return {'group_name': '测试群'}
            raise AssertionError(action)

        channel.request = fake_request
        channel._ws = object()  # 假装已连接：核验分支需要 ws 存在
        event = {'message_type': 'group', 'group_id': 88, 'user_id': 456,
                 'sender': {'nickname': '阿明', 'role': 'member'}}
        asyncio.run(channel._enrich_group_identity(event))
        self.assertEqual(event['_role'], {'role': 'admin', 'verified': True,
                                          'declared': 'member', 'source': 'api'})
        self.assertEqual(event['group_name'], '测试群')
        self.assertEqual(actor_from_event(event), 'qq:group:88:user:456@admin')
        # 接口失败：退回事件声明，职位只作展示，绝不进 actor。
        async def boom(action, params):
            raise ConnectionError('napcat down')

        channel.request = boom
        channel._role_cache.clear()
        event2 = dict(event, sender={'nickname': '阿明', 'role': 'admin'})
        asyncio.run(channel._enrich_group_identity(event2))
        self.assertFalse(event2['_role']['verified'])
        self.assertEqual(actor_from_event(event2), 'qq:group:88:user:456')
        # 关闭核验开关时连接口都不打，只用声明值展示。
        config.save_config({'channels': {'napcat': {'group_role_verify': False}}})
        channel._role_cache.clear()
        event3 = dict(event, sender={'nickname': '阿明', 'role': 'admin'})
        asyncio.run(channel._enrich_group_identity(event3))
        self.assertEqual(event3['_role'], {'role': 'admin', 'verified': False,
                                           'declared': 'admin', 'source': 'declared'})

    def test_group_collaboration_decision_uses_the_actual_sender(self):
        from main import MiniBotApp
        app = MiniBotApp.__new__(MiniBotApp)
        app.memory = self.memory
        app.jev = MagicMock()
        app.jev.is_configured.return_value = True
        app.jev.decide.return_value = {'help': 0}
        scope = 'qq:group:88:user:123'
        app._qq_collaborate('继续', scope, scope, 'qq_group', '88', self.event)
        state = json.loads(app.jev.decide.call_args.args[0])
        self.assertEqual(state['context']['sender']['user_id'], '123')
