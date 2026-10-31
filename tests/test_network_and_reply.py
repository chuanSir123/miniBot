import asyncio
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

from test_core import Fixture
from core import config
from core.network import urlopen
from core.process import clean_environment
from channels.qq_reply import reply_parts
from channels.napcat_channel import NapCatChannel


class DirectNetworkTests(unittest.TestCase):
    def test_http_never_discovers_system_or_environment_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'direct')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch('urllib.request.getproxies', side_effect=AssertionError('automatic proxy discovery')):
                with urlopen(f'http://127.0.0.1:{server.server_port}/', timeout=2) as response:
                    self.assertEqual(response.read(), b'direct')
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_child_environment_explicitly_bypasses_proxies(self):
        env = clean_environment({'HTTP_PROXY': 'http://127.0.0.1:7890', 'https_proxy': 'http://127.0.0.1:7890',
                                 'ALL_PROXY': 'socks5://localhost:7890', 'NO_PROXY': 'localhost', 'PATH': 'test'})
        self.assertEqual(env, {'PATH': 'test', 'NO_PROXY': '*', 'no_proxy': '*'})


class OutgoingMediaTests(Fixture):
    def setUp(self):
        super().setUp()
        config.save_config({'channels': {'napcat': {'owner_ids': ['1'], 'allow_owner_tools': True}}})
        self.event = {'message_type': 'private', 'user_id': 1}

    def test_markdown_media_and_local_file_names_with_spaces(self):
        for suffix, kind in (('.png', 'image'), ('.mp3', 'record'), ('.mp4', 'video')):
            path = self.root / ('my file' + suffix)
            if kind == 'image':
                from PIL import Image
                Image.new('RGB', (64, 96), 'red').save(path)
            else:
                path.write_bytes(b'local attachment')
            parts = reply_parts('给你：\n![附件](' + path.as_uri() + ')\n完成', self.event)
            self.assertEqual([p['type'] for p in parts], ['text', kind, 'text'])
            self.assertEqual(parts[1]['data']['file'], path.as_uri())

    def test_remote_and_data_sources_and_structured_reply(self):
        parts = reply_parts({'text': '附件', 'attachments': [
            {'type': 'audio', 'file': 'https://example.com/audio.mp3'},
            {'type': 'video', 'file': 'https://example.com/video.mp4'},
            {'type': 'image', 'file': 'data:image/png;base64,aW1hZ2U='},
        ]}, self.event)
        self.assertEqual([p['type'] for p in parts], ['text', 'record', 'video', 'image'])
        self.assertEqual(parts[-1]['data']['file'], 'base64://aW1hZ2U=')
        self.assertEqual(reply_parts('[视频](https://example.com/download?id=1)', self.event)[0]['type'], 'video')

    def test_local_files_require_owner_and_missing_files_fail_honestly(self):
        path = self.root / 'private.png'
        path.write_bytes(b'file')
        for event in ({'message_type': 'group', 'group_id': 2, 'user_id': 9},
                      {'message_type': 'private', 'user_id': 9}):
            parts = reply_parts('![图片](' + path.as_uri() + ')', event)
            self.assertEqual(parts[0]['type'], 'text')
            self.assertIn('无权', parts[0]['data']['text'])
        parts = reply_parts('![图片](' + (self.root / 'missing.png').as_uri() + ')', self.event)
        self.assertIn('附件发送失败', parts[0]['data']['text'])
        text = '```\n![图片](https://example.com/a.png)\n```'
        self.assertEqual(reply_parts(text, self.event), [{'type': 'text', 'data': {'text': text}}])

    def test_group_owner_can_send_local_image_without_trusting_nickname_or_quote(self):
        from PIL import Image
        path = self.root / 'owner.png'
        Image.new('RGB', (64, 96), 'red').save(path)
        event = {'message_type': 'group', 'group_id': 2, 'user_id': 1}
        text = '![图片](' + path.as_uri() + ')'
        self.assertEqual(reply_parts(text, event)[0]['type'], 'image')
        fake = dict(event, user_id=9, sender={'nickname': '主人1'}, _parsed={'reply_to_bot': True})
        self.assertEqual(reply_parts(text, fake)[0]['type'], 'text')
        config.save_config({'channels': {'napcat': {'allow_owner_tools': False}}})
        self.assertEqual(reply_parts(text, event)[0]['type'], 'text')

    def test_channel_emits_native_segments_and_reports_send_failure(self):
        channel = NapCatChannel()
        text = '![图片](https://example.com/a.png)[语音](https://example.com/a.mp3)[视频](https://example.com/a.mp4)'
        with patch.object(channel, '_send_segments', new_callable=AsyncMock, side_effect=[True, True, False]) as send, \
             patch.object(channel, '_send', new_callable=AsyncMock, return_value=True) as fallback:
            asyncio.run(channel.send_reply_segments(self.event, text))
        self.assertEqual([c.args[2][0]['type'] for c in send.call_args_list], ['image', 'record', 'video'])
        fallback.assert_awaited_once()
        self.assertIn('附件发送失败', fallback.call_args.args[2])

    def test_media_crosses_websocket_as_native_onebot_payload(self):
        channel = NapCatChannel()
        payloads = []

        async def run():
            channel._loop = asyncio.get_running_loop()

            async def acknowledge(raw):
                payload = json.loads(raw)
                payloads.append(payload)
                channel._pending[payload['echo']].set_result({'status': 'ok', 'retcode': 0})

            channel._ws = AsyncMock()
            channel._ws.send.side_effect = acknowledge
            await channel.send_reply_segments(self.event, {'attachments': [
                {'type': 'image', 'file': 'https://example.com/a.png'},
                {'type': 'audio', 'file': 'https://example.com/a.mp3'},
                {'type': 'video', 'file': 'https://example.com/a.mp4'},
            ]})

        asyncio.run(run())
        self.assertEqual([p['params']['message'][0]['type'] for p in payloads], ['image', 'record', 'video'])
        self.assertTrue(all(p['action'] == 'send_private_msg' and p['params']['user_id'] == 1 for p in payloads))
        self.assertEqual(channel._pending, {})


if __name__ == '__main__':
    unittest.main()
