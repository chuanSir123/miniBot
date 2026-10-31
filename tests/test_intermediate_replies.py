import asyncio
import concurrent.futures
import json
from unittest.mock import AsyncMock, patch

from test_core import Fixture, FakeJev, FakePerception
from core import config
from core.harness import AgentHarness
from core.skill_manager import SkillManager
from channels.napcat_channel import NapCatChannel


class IntermediateReplyTests(Fixture):
    def event(self, uid=123, mid=1):
        return {'post_type': 'message', 'message_type': 'private', 'user_id': uid, 'self_id': 999,
                'message_id': mid, 'message': [{'type': 'text', 'data': {'text': '处理任务'}}]}

    def test_harness_emits_complete_tool_turn_before_execution_not_final_or_reasoning(self):
        path = self.root / 'created.txt'
        received = []

        class Brain:
            calls = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    yield 'reasoning_delta', 'PRIVATE_REASONING'
                    yield 'text_delta', '我先'
                    yield 'text_delta', '创建文件。'
                    return '', [{'id': 'write', 'type': 'function', 'function': {'name': 'write_file',
                        'arguments': json.dumps({'file_path': str(path), 'content': 'ok'})}}], {}
                yield 'text_delta', '完成了。'
                return '', [], {}

        def intermediate(text):
            self.assertFalse(path.exists())
            received.append(text)

        harness = AgentHarness(Brain(), FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        reply = harness.run('创建文件', on_intermediate=intermediate)
        self.assertEqual(received, ['我先创建文件。'])
        self.assertEqual(reply, '完成了。')
        self.assertTrue(path.exists())

    def test_qq_sends_progress_while_work_is_running_then_final(self):
        sent = []

        async def run():
            progress_received = asyncio.Event()

            async def callback(text, event):
                # Exercise the real worker-thread -> event-loop handoff.
                await asyncio.to_thread(event['_on_intermediate'], '我开始处理了。')
                await asyncio.wait_for(progress_received.wait(), 1)
                return '处理完成。'

            async def send(event, text):
                sent.append(text)
                if text == '我开始处理了。':
                    progress_received.set()
                return True

            channel = NapCatChannel(on_message_callback=callback)
            with patch.object(channel, 'send_reply_segments', side_effect=send):
                await channel._handle_onebot_event(self.event())

        asyncio.run(run())
        self.assertEqual(sent, ['我开始处理了。', '处理完成。'])

    def test_synchronous_callback_progress_is_flushed_and_final_deduplicated(self):
        def callback(text, event):
            event['_on_intermediate']('完整消息。')
            event['_on_intermediate']('完整消息。')
            return '完整消息。'

        channel = NapCatChannel(on_message_callback=callback)
        with patch.object(channel, 'send_reply_segments', new_callable=AsyncMock, return_value=True) as send:
            asyncio.run(channel._handle_onebot_event(self.event()))
        self.assertEqual([c.args[1] for c in send.call_args_list], ['完整消息。'])

    def test_failed_intermediate_does_not_suppress_identical_final(self):
        def callback(text, event):
            event['_on_intermediate']('完整消息。')
            return '完整消息。'

        channel = NapCatChannel(on_message_callback=callback)
        with patch.object(channel, 'send_reply_segments', new_callable=AsyncMock, side_effect=[False, True]) as send:
            asyncio.run(channel._handle_onebot_event(self.event()))
        self.assertEqual(send.await_count, 2)

    def test_intermediate_failure_keeps_final_and_parallel_chats_are_isolated(self):
        sent = []

        async def callback(text, event):
            event['_on_intermediate'](f"开始{event['user_id']}")
            await asyncio.sleep(0)
            return f"完成{event['user_id']}"

        async def send(event, text):
            sent.append((event['user_id'], text))
            if text == '开始123':
                raise ConnectionError('send failed')
            return True

        channel = NapCatChannel(on_message_callback=callback)

        async def run():
            with patch.object(channel, 'send_reply_segments', side_effect=send):
                await asyncio.gather(channel._handle_onebot_event(self.event(123, 1)), channel._handle_onebot_event(self.event(456, 2)))

        asyncio.run(run())
        self.assertEqual([t for uid, t in sent if uid == 123], ['开始123', '完成123'])
        self.assertEqual([t for uid, t in sent if uid == 456], ['开始456', '完成456'])

    def test_main_wires_qq_intermediate_callback(self):
        from main import MiniBotApp
        import threading
        app = MiniBotApp.__new__(MiniBotApp)
        app.memory = self.memory
        app.pet_widget = None
        app._stop = threading.Event()

        class Harness:
            def run(inner, *args, **kwargs):
                kwargs['on_intermediate']('工具执行前说明')
                return '最终结果'

        app.harness = Harness()
        received = []
        event = dict(self.event(), _on_intermediate=received.append)
        app._handle_user_query('处理任务', 'qq:private:123', 'qq:private:123', 'qq_private', '123', False, [], event)
        self.assertEqual(received, ['工具执行前说明'])

    def test_group_future_callback_streams_before_future_completes(self):
        config.save_config({'channels': {'napcat': {'allowed_group_ids': ['88'], 'group_members_all': True}}})
        sent = []

        async def run():
            ready, arrived = asyncio.Event(), asyncio.Event()
            future = concurrent.futures.Future()
            source = {}

            def callback(text, event):
                source.update(event)
                ready.set()
                return future

            async def send(event, text):
                self.assertEqual(event['group_id'], 88)
                sent.append(text)
                arrived.set()
                return True

            channel = NapCatChannel(on_message_callback=callback)
            event = dict(self.event(), message_type='group', group_id=88,
                         message=[{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': '处理任务'}}])
            with patch.object(channel, 'send_reply_segments', side_effect=send):
                task = asyncio.create_task(channel._handle_onebot_event(event))
                await asyncio.wait_for(ready.wait(), 1)
                await asyncio.to_thread(source['_on_intermediate'], '开始调用工具。')
                await asyncio.wait_for(arrived.wait(), 1)
                self.assertFalse(future.done())
                future.set_result('群聊最终结果。')
                await task

        asyncio.run(run())
        self.assertEqual(sent, ['开始调用工具。', '群聊最终结果。'])

    def test_chat_mode_splits_text_into_segments_but_tool_mode_does_not(self):
        channel = NapCatChannel()
        sent_chat = []
        sent_tool = []

        async def fake_send_chat(action, target, segment):
            sent_chat.append(segment)
            return True

        async def fake_send_tool(action, target, segment):
            sent_tool.append(segment)
            return True

        sample_text = "主人下午好呀！今天的天气真不错呢。要一起出去散散步吗？"

        # 1. chat 模式：未调用工具，文本被拆分成多段发送
        chat_event = dict(self.event(), _used_tools=False)
        with patch.object(channel, '_send', side_effect=fake_send_chat):
            asyncio.run(channel._send_text_reply(chat_event, sample_text))
        self.assertGreater(len(sent_chat), 1)

        # 2. 工具调用模式：调用过工具，整段直接发出，不拆分
        tool_event = dict(self.event(), _used_tools=True)
        with patch.object(channel, '_send', side_effect=fake_send_tool):
            asyncio.run(channel._send_text_reply(tool_event, sample_text))
        self.assertEqual(len(sent_tool), 1)
        self.assertEqual(sent_tool[0], sample_text)

    def test_media_ref_sent_only_once_per_turn_and_not_leaked_in_intermediate(self):
        from core.media_store import MediaStore
        from test_media_store import mp4_bytes
        config.save_config({'channels': {'napcat': {'owner_ids': [123]}}})
        store = MediaStore()
        video_asset = store.add(mp4_bytes(), 'local', origin='registered', label='test.mp4')
        media_ref = video_asset['media_ref']

        channel = NapCatChannel()
        sent_messages = []
        sent_media_segments = []

        async def fake_send_text(action, target, message):
            sent_messages.append(message)
            return True

        async def fake_send_segments(action, target, segments, media_asset=None, asset_scope=None):
            for s in segments:
                if s.get("type") in ("image", "video", "record"):
                    sent_media_segments.append(s)
            return True

        with patch.object(channel, '_send', side_effect=fake_send_text),              patch.object(channel, '_send_segments', side_effect=fake_send_segments):
            
            event = dict(self.event())
            text_with_media = f"这是做好的视频：\n![视频](media://{media_ref})"

            # 1. 中间消息发送：不应发送任何媒体附件
            event["_is_intermediate"] = True
            asyncio.run(channel.send_reply_segments(event, text_with_media))
            event.pop("_is_intermediate", None)
            self.assertEqual(len(sent_media_segments), 0)

            # 2. 最终回复发送：媒体附件被首次送出
            asyncio.run(channel.send_reply_segments(event, text_with_media))
            self.assertEqual(len(sent_media_segments), 1)

            # 3. 模拟重复调用最终发送：相同 media_ref 在同一轮中绝对不发第二次
            asyncio.run(channel.send_reply_segments(event, text_with_media))
            self.assertEqual(len(sent_media_segments), 1)

    def test_rewritten_repeat_is_deduped_while_new_object_progress_passes(self):
        sent = []

        async def callback(text, event):
            on_progress = event['_on_intermediate']
            # 1. 正常进度：发出
            on_progress("工作空间只有 ego 文件夹，没有旧的鹈鹕文件，喵喵这就写一个 HTML 动画")
            # 2. 换了个说法把同一件事再讲一遍：判重拦下
            on_progress("工作空间只有 ego 文件夹，旧的鹈鹕文件之前删掉了，喵喵这就写一个 HTML 动画")
            # 3. 同模板但换了对象：属于新进展，必须放行
            on_progress("正在读取 config.json")
            on_progress("正在读取 package.json")
            return "最终完整答复：主人，鹈鹕骑自行车的 MP4 做好啦！"

        async def fake_send_segments(event, text):
            sent.append(text)
            return True

        channel = NapCatChannel(on_message_callback=callback)
        with patch.object(channel, 'send_reply_segments', side_effect=fake_send_segments):
            asyncio.run(channel._handle_onebot_event(self.event()))

        self.assertEqual(len(sent), 4)
        self.assertIn("没有旧的鹈鹕文件", sent[0])
        self.assertEqual(sent[1], "正在读取 config.json")
        self.assertEqual(sent[2], "正在读取 package.json")
        self.assertIn("最终完整答复", sent[3])

    def test_reordered_but_identical_words_are_not_treated_as_repeat(self):
        sent = []

        async def callback(text, event):
            on_progress = event['_on_intermediate']
            on_progress("本喵这就去给你的资料页点个赞")
            on_progress("给你的资料页点个赞本喵这就去")
            return "好了。"

        async def fake_send_segments(event, text):
            sent.append(text)
            return True

        channel = NapCatChannel(on_message_callback=callback)
        with patch.object(channel, 'send_reply_segments', side_effect=fake_send_segments):
            asyncio.run(channel._handle_onebot_event(self.event()))

        # 旧的字符集合 Jaccard 会把这两句判成同一句（集合完全相同），顺序敏感后不再误杀。
        self.assertIn("给你的资料页点个赞本喵这就去", sent)

    def test_progress_carrying_media_ref_is_held_for_final_delivery(self):
        sent = []

        async def callback(text, event):
            on_progress = event['_on_intermediate']
            on_progress("视频做好啦～\n![视频](media://vid_abc123)")
            on_progress("还在收尾")
            return "视频做好啦～"

        async def fake_send_segments(event, text):
            sent.append(text)
            return True

        channel = NapCatChannel(on_message_callback=callback)
        with patch.object(channel, 'send_reply_segments', side_effect=fake_send_segments):
            asyncio.run(channel._handle_onebot_event(self.event()))

        self.assertNotIn("视频做好啦～\n![视频](media://vid_abc123)", sent)
        self.assertIn("还在收尾", sent)

