import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from unittest.mock import AsyncMock, patch

try:
    from PIL import Image
except ImportError:  # 媒体库本身不依赖 Pillow，测试也应在无 Pillow 环境跑通
    Image = None
from test_core import Fixture, FakeJev, FakePerception
from channels.qq_media import MessageParser
from channels.qq_reply import reply_parts
from channels.napcat_channel import NapCatChannel
from core import config
from core.decision_context import build_decision_context
from core.harness import AgentHarness
from core.image_service import execute
from core.media_store import MediaStore
from core.skill_manager import SkillManager
from core.workspace import workspace_for

_COLORS = {'red': (255, 0, 0, 255), 'blue': (0, 0, 255, 255), 'green': (0, 128, 0, 255)}


def _minimal_png(width, height, rgba=(255, 0, 0, 255)):
    """无 Pillow 时手写合法 PNG，保证媒体库用例仍可运行。"""
    import struct
    import zlib

    row = bytes(rgba) * width
    raw = (b'\x00' + row) * height

    def chunk(tag, payload):
        return struct.pack('>I', len(payload)) + tag + payload + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))


def png(color='red', size=(96, 128)):
    if Image is not None:
        output = BytesIO()
        Image.new('RGBA', size, color).save(output, 'PNG')
        return output.getvalue()
    rgba = color if isinstance(color, tuple) else _COLORS.get(color, (255, 0, 0, 255))
    return _minimal_png(size[0], size[1], rgba)


def mp4_bytes():
    return b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41' + b'\x00' * 64


def mp3_bytes():
    return b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 64


class MediaReferenceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = MediaStore()
        self.scope = 'qq:private:123'
        self.raw = png()
        self.asset = self.store.add(self.raw, self.scope, message_id='5')
        self.event = {'message_type': 'private', 'user_id': 123, 'self_id': 999, 'message_id': 6}

    def runtime(self, scope=None):
        scope = scope or self.scope
        return {'media_scope': scope, 'media_root': str(self.store.root), 'workspace': str(workspace_for(scope))}

    def test_original_bytes_dedup_scope_isolation_and_restart(self):
        self.assertEqual(self.store.add(self.raw, self.scope, message_id='5')['media_ref'], self.asset['media_ref'])
        other = self.store.add(self.raw, 'qq:private:456', message_id='5')
        self.assertNotEqual(other['media_ref'], self.asset['media_ref'])
        self.assertEqual(len(list((self.store.root / 'blobs').glob('*.png'))), 1)
        restarted = MediaStore(self.store.root)
        self.assertEqual(restarted.get(self.asset['media_ref'], self.scope)[1], self.raw)
        with self.assertRaises(ValueError):
            restarted.get(self.asset['media_ref'], 'qq:private:456')
        group = self.store.add(self.raw, 'qq:group:88:user:123')
        self.assertEqual(restarted.get(group['media_ref'], 'qq:group:88:user:456')[1], self.raw)
        with self.assertRaises(ValueError):
            restarted.get(group['media_ref'], 'qq:group:99:user:123')
        self.assertNotIn('digest', self.asset)
        self.assertNotIn('base64', json.dumps(self.asset))

    def test_missing_corrupt_and_expired_refs_never_become_generation(self):
        ref = self.asset['media_ref']
        with self.store._db() as db:
            db.execute('UPDATE media SET expires_at=? WHERE media_ref=?', (time.time() - 1, ref))
        with patch('core.image_service.urlopen') as api:
            result = execute({'mode': 'edit', 'prompt': '改背景', 'image_refs': [ref], '_minibot': self.runtime()})
        self.assertFalse(result['ok'])
        api.assert_not_called()
        asset = self.store.add(png('blue'), self.scope)
        self.store.path(asset['media_ref'], self.scope).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            self.store.get(asset['media_ref'], self.scope)

    def test_prune_cleans_expired_blobs(self):
        with self.store._db() as db:
            db.execute('UPDATE media SET expires_at=0')
        self.store.prune()
        self.assertEqual(self.store.list(self.scope), [])
        self.assertEqual(list((self.store.root / 'blobs').iterdir()), [])

    def test_parser_caches_original_and_marks_quoted_message(self):
        url = 'data:image/png;base64,' + base64.b64encode(self.raw).decode()
        event = dict(self.event, message=[{'type': 'reply', 'data': {'id': '5'}},
                                        {'type': 'text', 'data': {'text': '把这张图改成蓝色'}}])
        rpc = AsyncMock(return_value={'status': 'ok', 'retcode': 0, 'data': {
            'message_id': 5, 'sender': {'user_id': 999}, 'message': [{'type': 'image', 'data': {'file': url}}]}})
        result = asyncio.run(MessageParser(rpc, event).parse())
        self.assertEqual(len(result['media']), 1)
        asset = result['media'][0]
        self.assertEqual(asset['origin'], 'quoted')
        self.assertEqual(asset['message_id'], '5')
        self.assertEqual(asset['sender_qq'], '999')
        self.assertEqual(self.store.get(asset['media_ref'], self.scope)[1], self.raw)
        self.assertIn(asset['media_ref'], result['text'])
        self.assertNotIn('base64', result['text'])
        self.assertTrue(result['images'][0].startswith('data:image/jpeg;base64,'))
        self.assertEqual(result['quoted_messages'][0]['sender_qq'], '999')
        context = build_decision_context(self.memory, self.scope, self.scope, dict(event, _parsed=result))
        self.assertEqual(context['current_media'][0]['media_ref'], asset['media_ref'])
        self.assertEqual(context['quoted_messages'][0]['sender_qq'], '999')

    def test_nine_references_resolve_in_order_and_output_is_reusable(self):
        refs, originals = [], []
        for i in range(9):
            raw = png((i * 20, 20, 30, 128))
            originals.append(raw)
            refs.append(self.store.add(raw, self.scope)['media_ref'])
        response = BytesIO(json.dumps({'data': [{'b64_json': base64.b64encode(png('green')).decode()}]}).encode())
        with patch('core.image_service.urlopen', return_value=response) as api, \
                patch('core.image_service.ENDPOINT', 'http://127.0.0.1/image'):
            result = execute({'mode': 'edit', 'prompt': '保持画风，只改背景', 'image_refs': refs, '_minibot': self.runtime()})
        self.assertTrue(result['ok'], result)
        payload = json.loads(api.call_args.args[0].data)
        self.assertEqual([base64.b64decode(value.partition(',')[2]) for value in payload['images']], originals)
        self.assertEqual(payload['prompt'], '保持画风，只改背景')
        self.assertEqual((payload['width'], payload['height']), (96, 128))
        generated = result['assets'][0]
        self.assertTrue(generated['path'].startswith(str(workspace_for(self.scope))))
        self.assertNotIn('base64', json.dumps(result))
        self.assertEqual(self.store.get(generated['media_ref'], self.scope)[1], png('green'))

    def test_edit_requires_real_refs_and_generate_rejects_refs(self):
        cases = [dict(mode='edit'), dict(mode='generate', image_refs=[self.asset['media_ref']]),
                 dict(mode='edit', image_refs=['img_' + '0' * 16]),
                 dict(mode='edit', image_refs=[self.asset['media_ref']] * 10)]
        with patch('core.image_service.urlopen') as api:
            for case in cases:
                result = execute({'prompt': '修改', '_minibot': self.runtime(), **case})
                self.assertFalse(result['ok'], result)
        api.assert_not_called()

    def test_media_uri_becomes_native_qq_image_without_leaking_internal_fields(self):
        text = '![图片](media://' + self.asset['media_ref'] + ')'
        parts = reply_parts(text, self.event)
        self.assertEqual(parts[0]['type'], 'image')
        self.assertTrue(parts[0]['data']['file'].startswith('file:'))
        channel = NapCatChannel()
        with patch.object(channel, '_send_segments', new_callable=AsyncMock, return_value=True) as send:
            asyncio.run(channel.send_reply_segments(self.event, text))
        sent = send.call_args.args[2][0]
        self.assertEqual(set(sent), {'type', 'data'})
        wrong = reply_parts(text, dict(self.event, user_id=456))
        self.assertEqual(wrong[0]['type'], 'text')
        self.assertIn('附件发送失败', wrong[0]['data']['text'])

    def test_notification_sends_media_separately_and_never_puts_ref_in_text(self):
        """定时任务/主动提醒：占位符不上屏，图片单独发一条。

        群里出现过「正文里跟着 ![图片](media://img_x) 原文、图片却没发」——推送
        路径走的是纯文本 `_send`，根本没解析占位。这里把这条路径钉住。
        """
        ref = self.asset['media_ref']
        text = '点赞好啦\n![图片](media://' + ref + ')'
        channel = NapCatChannel()

        async def run():
            channel._loop = asyncio.get_running_loop()

            async def acknowledge(raw):
                payload = json.loads(raw)
                channel._pending[payload['echo']].set_result(
                    {'status': 'ok', 'retcode': 0, 'data': {'message_id': 7}})

            channel._ws = AsyncMock()
            channel._ws.send.side_effect = acknowledge
            return await channel._send_notification_async('qq_private', '123', text)

        self.assertTrue(asyncio.run(run()))
        payloads = [json.loads(c.args[0]) for c in channel._ws.send.call_args_list]
        segments = [m['params']['message'][0] for m in payloads]
        self.assertEqual([s['type'] for s in segments], ['text', 'image'])
        self.assertEqual(segments[0]['data']['text'], '点赞好啦')
        self.assertNotIn('media://', segments[0]['data']['text'])
        self.assertTrue(segments[1]['data']['file'].startswith('file:'))
        # 媒体发送成功后要和消息 id 绑定，后续引用才能复用
        self.assertEqual(self.store.list(self.scope, message_id='7')[0]['media_ref'], ref)

    def test_strip_media_links_removes_placeholder_only(self):
        from core.media_store import strip_media_links

        self.assertEqual(strip_media_links('好耶\n![图片](media://img_x)\n'), '好耶')
        self.assertEqual(strip_media_links('没有媒体的正文'), '没有媒体的正文')
        self.assertEqual(strip_media_links(None), '')

    def test_workspace_relative_writes_and_register_preview_tools(self):
        harness = AgentHarness(None, FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        result = harness._dispatch_tool('write', {'file_path': 'notes/output.txt', 'content': 'workspace'}, scope='local')
        self.assertTrue(result['ok'])
        self.assertEqual((workspace_for('local') / 'notes/output.txt').read_text(), 'workspace')
        image = workspace_for('local') / 'sample.png'
        image.write_bytes(self.raw)
        asset = harness._dispatch_tool('register_media', {'file_path': 'sample.png'})
        self.assertTrue(asset['ok'])
        listing = harness._dispatch_tool('list_media', {})
        self.assertEqual(listing['media'][0]['media_ref'], asset['media_ref'])
        preview = harness._dispatch_tool('get_media', {'media_ref': asset['media_ref']})
        if Image is not None:
            self.assertTrue(preview['_image'].startswith(b'\xff\xd8'))
        else:
            # 无 Pillow 时 preview 退化为原始字节，不阻断发送链路
            self.assertTrue(preview['_image'].startswith(b'\x89PNG'))

    def test_video_and_audio_refs_keep_kind_and_become_native_qq_segments(self):
        video = self.store.add(mp4_bytes(), self.scope, origin='registered', label='clip.mp4')
        audio = self.store.add(mp3_bytes(), self.scope, origin='registered', label='voice.mp3')
        self.assertTrue(video['media_ref'].startswith('vid_'))
        self.assertTrue(audio['media_ref'].startswith('aud_'))
        self.assertEqual(video['mime'], 'video/mp4')
        self.assertEqual(audio['mime'], 'audio/mpeg')
        self.assertEqual(self.store.get(video['media_ref'], self.scope)[1], mp4_bytes())
        self.assertIsNone(self.store.preview(video['media_ref'], self.scope))

        # 模型把标签写错也必须按 media_ref 的真实类型发送，不能把 mp4 塞进 image 段
        parts = reply_parts('![图片](media://' + video['media_ref'] + ')', self.event)
        self.assertEqual(parts[0]['type'], 'video')
        self.assertTrue(parts[0]['data']['file'].startswith('file:'))
        audio_parts = reply_parts('![语音](media://' + audio['media_ref'] + ')', self.event)
        self.assertEqual(audio_parts[0]['type'], 'record')
        # 无中文标签时同样按前缀识别
        bare = reply_parts('![附件](media://' + video['media_ref'] + ')', self.event)
        self.assertEqual(bare[0]['type'], 'video')

    def test_register_media_accepts_mp4_and_rejects_unknown_binary(self):
        harness = AgentHarness(None, FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        clip = workspace_for('local') / 'pelican_bike.mp4'
        clip.write_bytes(mp4_bytes())
        asset = harness._dispatch_tool('register_media', {'file_path': 'pelican_bike.mp4'})
        self.assertTrue(asset['ok'], asset)
        self.assertEqual(asset['kind'], 'video')
        self.assertTrue(asset['media_ref'].startswith('vid_'))
        self.assertEqual(harness._dispatch_tool('get_media', {'media_ref': asset['media_ref']})['kind'], 'video')
        self.assertNotIn('_image', harness._dispatch_tool('get_media', {'media_ref': asset['media_ref']}))
        junk = workspace_for('local') / 'notes.bin'
        junk.write_bytes(b'\x00' * 64)
        rejected = harness._dispatch_tool('register_media', {'file_path': 'notes.bin'})
        self.assertFalse(rejected['ok'])
        self.assertIn('媒体', rejected['error'])

    def test_actual_skill_worker_resolves_refs_without_stdout_base64(self):
        received = []
        output_image = png('green')

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                data = json.dumps({'data': [{'b64_json': base64.b64encode(output_image).decode()}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            manager = SkillManager(self.root / 'skills')
            code = f'''def execute(context):
    from core import image_service
    image_service.ENDPOINT = 'http://127.0.0.1:{server.server_port}/images'
    return image_service.execute(context)
'''
            manager.register_new_skill('image_gen.py', code, activate=True)
            result = manager.execute_skill('image_gen', {'prompt': '只修改背景', 'mode': 'edit', 'image_refs': [self.asset['media_ref']]}, media_scope=self.scope)
            self.assertTrue(result['ok'], result)
            self.assertNotIn('b64_json', json.dumps(result))
            self.assertEqual(base64.b64decode(received[0]['images'][0].partition(',')[2]), self.raw)
            self.assertTrue(self.store.get(result['assets'][0]['media_ref'], self.scope)[1] == output_image)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_dynamic_skill_expands_media_refs_through_injected_helpers(self):
        """动态技能拿到的是 media_ref，必须能靠 _minibot 助手自己展开成图片数据。"""
        manager = SkillManager(self.root / 'skills')
        code = '''def execute(context):
    helpers = context["_minibot"]
    refs = context.get("image_refs") or []
    resolved = helpers["resolve_media_refs"](refs)
    single = helpers["data_url"]("media://" + refs[0])
    data = helpers["media_bytes"]("media://" + refs[0])
    return {"ok": True, "count": len(resolved), "same": resolved[0] == single,
            "is_data_url": resolved[0].startswith("data:image/"),
            "bytes_ok": data[:8] == b"\\x89PNG\\r\\n\\x1a\\n",
            "raw_ref_leaked": refs[0] in resolved[0],
            "path_ok": helpers["media_path"](refs[0]).endswith(".png")}
'''
        manager.register_new_skill('media_echo.py', code, activate=True)
        result = manager.execute_skill('media_echo', {'image_refs': [self.asset['media_ref']]}, media_scope=self.scope)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['count'], 1)
        self.assertTrue(result['same'], result)
        self.assertTrue(result['is_data_url'], result)
        self.assertTrue(result['bytes_ok'], result)
        self.assertFalse(result['raw_ref_leaked'], result)
        self.assertTrue(result['path_ok'], result)
        self.assertNotIn('base64', json.dumps(result))

    def test_media_refs_tolerate_media_url_prefix_and_padding(self):
        ref = self.asset['media_ref']
        self.assertEqual(self.store.get('media://' + ref, self.scope)[0]['media_ref'], ref)
        self.assertEqual(self.store.get('  ' + ref + '  ', self.scope)[0]['media_ref'], ref)
        with self.assertRaises(ValueError):
            self.store.get('media://not-a-ref', self.scope)

    def test_send_receipt_binds_reference_and_quote_reuses_cached_original(self):
        channel = NapCatChannel()

        async def send():
            channel._loop = asyncio.get_running_loop()

            async def acknowledge(raw):
                payload = json.loads(raw)
                channel._pending[payload['echo']].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': 55}})

            channel._ws = AsyncMock()
            channel._ws.send.side_effect = acknowledge
            await channel.send_reply_segments(self.event, '![图片](media://' + self.asset['media_ref'] + ')')

        asyncio.run(send())
        self.assertEqual(self.store.list(self.scope, message_id='55')[0]['media_ref'], self.asset['media_ref'])
        self.store.bind_message(self.asset['media_ref'], self.scope, '56')
        self.assertEqual(self.store.list(self.scope, message_id='55')[0]['media_ref'], self.asset['media_ref'])
        event = dict(self.event, message_id=57, message=[{'type': 'reply', 'data': {'id': '55'}}])
        rpc = AsyncMock(return_value={'status': 'ok', 'data': {'message_id': 55, 'sender': {'user_id': 999},
            'message': [{'type': 'image', 'data': {'url': 'https://example.com/expired.png'}}]}})
        with patch('channels.qq_media.media_bytes') as download:
            parsed = asyncio.run(MessageParser(rpc, event).parse())
        download.assert_not_called()
        self.assertEqual(parsed['media'][0]['media_ref'], self.asset['media_ref'])

    def test_generic_skill_writes_to_session_workspace(self):
        manager = SkillManager(self.root / 'skills')
        manager.register_new_skill('writer.py', '''def execute(context):
    from pathlib import Path
    Path('artifact.txt').write_text('workspace output')
    return {'ok': True, 'path': str(Path('artifact.txt').resolve())}
''', activate=True)
        result = manager.execute_skill('writer', {}, media_scope=self.scope)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['path'], str(workspace_for(self.scope) / 'artifact.txt'))

    def test_group_owner_has_full_tools_and_nonowner_is_still_denied(self):
        config.save_config({'channels': {'napcat': {'owner_ids': ['123'], 'allow_owner_tools': True}}})
        manager = SkillManager(self.root / 'skills')
        manager.register_new_skill('image_gen.py', 'def execute(context): return {"ok": True, "output": "edited"}', activate=True)

        class Brain:
            calls = 0

            def stream_chat_and_tools(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    # 技能已改纯 Markdown 指令：模型侧不再有技能执行工具。
                    names = [tool['function']['name'] for tool in tools]
                    self.assertNotIn('execute_skill', names)
                    # 工具名与 pi 一致：schema 里就是实现名，没有别名层。
                    self.assertIn('bash', names)
                    self.assertIn('read', names)
                    self.assertIn('edit', names)
                    self.assertIn('write', names)
                    self.assertIn('image_gen', messages[0]['content'])
                    return '', [{'id': 'edit', 'type': 'function', 'function': {
                        'name': 'list_skills', 'arguments': '{}'}}], {}
                yield 'text_delta', '已处理'
                return '已处理', [], {}

        harness = AgentHarness(Brain(), FakeJev(), self.memory, FakePerception(), manager)
        scope = 'qq:group:88:user:123'
        from main import MiniBotApp
        event = {'message_type': 'group', 'group_id': 88, 'user_id': 123}
        self.assertTrue(MiniBotApp.identity(event)[4])
        self.assertFalse(MiniBotApp.identity(dict(event, user_id=456))[4])
        harness.run('修改引用的图片', scope=scope, session_id=scope, channel='qq_group', allow_tools=MiniBotApp.identity(event)[4])
        tool_results = [json.loads(m['content']) for m in self.memory.get_history(scope)[0] if m['role'] == 'tool']
        self.assertTrue(tool_results[0]['ok'])
        self.assertTrue(harness._dispatch_tool('write', {'file_path': 'owner.txt', 'content': 'x'}, scope=scope)['ok'])
        self.assertFalse(harness._dispatch_tool('write', {'file_path': 'other.txt', 'content': 'x'}, scope='qq:group:88:user:456')['ok'])
        self.assertFalse(harness._dispatch_tool('execute_skill', {'skill_name': 'image_gen'}, scope='qq:group:88:user:456')['ok'])
        config.save_config({'channels': {'napcat': {'allow_owner_tools': False}}})
        self.assertFalse(MiniBotApp.identity(event)[4])
        self.assertFalse(harness._dispatch_tool('write', {'file_path': 'revoked.txt', 'content': 'x'}, scope=scope)['ok'])
        self.assertFalse(harness._dispatch_tool('execute_skill', {'skill_name': 'image_gen'}, scope=scope)['ok'])

    def test_fabricated_media_reply_is_not_reported_as_success(self):
        class Brain:
            def stream_chat_and_tools(self, *args, **kwargs):
                yield 'text_delta', '已经改好了！![图片](media://img_0000000000000000)'
                return '', [], {}

        harness = AgentHarness(Brain(), FakeJev(), self.memory, FakePerception(), SkillManager(self.root / 'skills'))
        result = harness.run('修改图片')
        self.assertNotIn('已经改好了', result)
        self.assertNotIn('media://', result)
        self.assertIn('没有可发送的结果', result)

    def test_quote_recovers_cached_image_when_get_msg_is_unavailable(self):
        event = dict(self.event, message=[{'type': 'reply', 'data': {'id': '5'}},
                                        {'type': 'text', 'data': {'text': '改背景'}}])
        rpc = AsyncMock(return_value={'status': 'failed', 'retcode': 1, 'message': 'message expired'})
        parsed = asyncio.run(MessageParser(rpc, event).parse())
        self.assertEqual(parsed['media'][0]['media_ref'], self.asset['media_ref'])
        self.assertEqual(parsed['media'][0]['origin'], 'quoted')
        self.assertEqual(parsed['direct_text'], '改背景')
        self.assertIn('正文暂不可获取', parsed['text'])

    def test_quote_cache_fallback_cannot_read_a_different_conversation(self):
        event = dict(self.event, user_id=456, message=[{'type': 'reply', 'data': {'id': '5'}}])
        rpc = AsyncMock(return_value={'status': 'failed', 'retcode': 1})
        parsed = asyncio.run(MessageParser(rpc, event).parse())
        self.assertEqual(parsed['media'], [])
        self.assertTrue(parsed['errors'])

    def test_owner_qq_can_send_media_registered_in_local_or_main_scope(self):
        # 1. 在 local 工作空间注册一个视频
        video_asset = self.store.add(mp4_bytes(), 'local', origin='registered', label='pelican_bike.mp4')
        self.assertEqual(video_asset['mime'], 'video/mp4')

        # 2. 配置主人 QQ
        owner_qq = 777888
        config.save_config({'channels': {'napcat': {'owner_ids': [owner_qq]}}})

        # 3. 为视频创建测试封面图
        video_path = self.store.path(video_asset['media_ref'], 'local')
        cover_path = video_path.with_name(video_path.stem + '.cover.jpg')
        cover_path.write_bytes(b'FAKE_COVER_JPEG')

        # 主人 QQ 私聊发送该视频
        owner_event = {'message_type': 'private', 'user_id': owner_qq, 'self_id': 999, 'message_id': 100}
        text = '主人，自行车视频做好啦～\n![视频](media://' + video_asset['media_ref'] + ')'
        parts = reply_parts(text, owner_event)

        # 校验分段：第一段为文本，第二段为视频段，且包含封面图
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]['type'], 'text')
        self.assertEqual(parts[1]['type'], 'video')
        self.assertTrue(parts[1]['data']['file'].startswith('file:'))
        self.assertIn('cover', parts[1]['data'])
        self.assertTrue(parts[1]['data']['cover'].startswith('file:'))
        self.assertEqual(parts[1]['_media']['media_ref'], video_asset['media_ref'])

        # 4. 主人 QQ 查询媒体列表也能看到该视频
        owner_media_list = self.store.list(f'qq:private:{owner_qq}')
        refs = [m['media_ref'] for m in owner_media_list]
        self.assertIn(video_asset['media_ref'], refs)

        # 5. 非主人 QQ 访问该媒体依然被租户隔离拦截
        non_owner_event = {'message_type': 'private', 'user_id': 999999, 'self_id': 999, 'message_id': 101}
        wrong_parts = reply_parts(text, non_owner_event)
        self.assertEqual(wrong_parts[1]['type'], 'text')
        self.assertIn('附件发送失败', wrong_parts[1]['data']['text'])

