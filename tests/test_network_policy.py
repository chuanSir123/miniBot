import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from core.network import urlopen
from core.process import clean_environment
from channels.napcat_channel import NapCatChannel


class NetworkPolicyTests(unittest.TestCase):
    def test_http_and_redirect_ignore_environment_and_system_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/ok')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'direct-ok')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bad = {k: 'http://127.0.0.1:1' for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')}
            bad.update(NO_PROXY='', no_proxy='')
            with patch.dict(os.environ, bad), patch('urllib.request.getproxies', side_effect=AssertionError('must not auto-detect proxies')):
                with urlopen(f'http://127.0.0.1:{server.server_port}/redirect', timeout=3) as response:
                    self.assertEqual(response.read(), b'direct-ok')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_child_environment_scrubs_all_proxy_variants(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'bad', 'ws_proxy': 'bad', 'FTP_PROXY': 'bad', 'PROXY': 'bad', 'NO_PROXY': ''}):
            env = clean_environment()
        self.assertEqual(env['NO_PROXY'], '*')
        self.assertEqual(env['no_proxy'], '*')
        self.assertFalse(any(k.upper().endswith('_PROXY') and k.upper() != 'NO_PROXY' for k in env))
        self.assertNotIn('PROXY', env)

    def test_remote_websocket_cannot_enable_auto_proxy(self):
        def connect(uri, *, proxy=True, **kwargs):
            pass
        with patch('channels.napcat_channel.connect', connect):
            options = NapCatChannel.connection_options({'ws_url': 'wss://example.org/ws', 'use_proxy': True})
        self.assertIsNone(options['proxy'])
