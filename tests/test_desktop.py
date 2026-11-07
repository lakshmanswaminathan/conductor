"""Desktop API boundaries and image transport regressions."""
import io
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from conductor.cli import initialize
from conductor.models import DEFAULT_MODEL
from conductor.provider import OpenRouter, validate_key
from conductor.ui_server import App, make_server
from conductor.store import Store


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)
        self.app = App(self.root / 'runs')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_settings_never_persist_or_return_key(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"data":{}}')) as request:
            result = self.app.update_settings({'api_key': '  test-credential  ', 'model': DEFAULT_MODEL})
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, 'https://openrouter.ai/api/v1/key')
        self.assertEqual(sent.get_header('Authorization'), 'Bearer test-credential')
        self.assertTrue(result['key_configured'])
        self.assertNotIn('test-credential', json.dumps(result))
        self.assertNotIn('test-credential', self.app.settings_path.read_text())
        self.assertEqual(self.app.settings_path.stat().st_mode & 0o777, 0o600)

    def test_rejected_key_preserves_existing_session_without_leaking_response(self):
        self.app.key = 'previous-key'
        error = urllib.error.HTTPError('https://openrouter.ai/api/v1/key', 401, 'Unauthorized', {}, io.BytesIO(b'sensitive-response'))
        with patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(ValueError, 'OpenRouter rejected the API key') as raised:
                self.app.update_settings({'api_key': 'rejected-test-key'})
            request.assert_called_once()
        self.assertNotIn('sensitive-response', str(raised.exception))
        self.assertNotIn('rejected-test-key', str(raised.exception))
        self.assertEqual(self.app.key, 'previous-key')
        self.assertFalse(self.app.settings_path.exists())

    def test_key_wrappers_rejected_before_network(self):
        with patch('urllib.request.urlopen') as request:
            for key in ['Bearer test-key', '"test-key"', 'export OPENROUTER_API_KEY=test-key', 'test\nkey']:
                with self.assertRaisesRegex(ValueError, 'Paste only'):
                    validate_key(key)
            request.assert_not_called()

    def test_chat_auth_failure_is_actionable_and_not_retried(self):
        provider = OpenRouter(DEFAULT_MODEL, key='test-key')
        error = urllib.error.HTTPError('https://openrouter.ai/api/v1/chat/completions', 401, 'Unauthorized', {}, io.BytesIO(b'sensitive-response'))
        with patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError, 'Open Settings') as raised:
                provider.complete([{'role': 'user', 'content': 'hello'}], [])
            request.assert_called_once()
            self.assertNotIn('sensitive-response', str(raised.exception))

    def test_truncated_tool_call_is_not_returned_for_execution(self):
        provider = OpenRouter(DEFAULT_MODEL, key='test-key')
        response = {'choices': [{'finish_reason': 'length', 'message': {'role': 'assistant',
            'tool_calls': [{'id': 'partial', 'function': {'name': 'cmux', 'arguments': '{'}}]}}]}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as request:
            with self.assertRaisesRegex(RuntimeError, 'incomplete output was not executed'):
                provider.complete([{'role': 'user', 'content': 'inspect cmux'}], [])
            request.assert_called_once()

    def test_auth_host_and_cross_origin_requests(self):
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            for headers, body, status in [({}, None, 401), ({'Host': 'attacker.example', 'X-Conductor-Token': self.app.token}, None, 401),
                                  ({'X-Conductor-Token': self.app.token, 'Origin': 'https://attacker.example'}, b'{}', 403)]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url + '/api/settings', data=body, headers=headers))
                self.assertEqual(error.exception.code, status)
                error.exception.close()
            request = urllib.request.Request(url + '/', headers={'X-Conductor-Token': self.app.token})
            with urllib.request.urlopen(request) as response:
                self.assertIn('HttpOnly', response.headers['Set-Cookie'])
                self.assertIn(b'Conductor', response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_rejected_finish_is_not_rendered_as_success(self):
        store = initialize(self.app.root / 'chat-test', self.root, 'Task', scope='filesystem', ui=True)
        try:
            for call_id, result in [('rejected', {'error': 'Workers active'}), ('accepted', {'summary': 'Finished'})]:
                store.append('coordinator', {'role': 'assistant', 'tool_calls': [{'id': call_id, 'function':
                    {'name': 'finish', 'arguments': json.dumps({'summary': call_id})}}]})
                store.append('coordinator', {'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result)})
            content = [m['content'] for m in self.app.snapshot('chat-test')['messages']]
            self.assertNotIn('rejected', content)
            self.assertIn('accepted', content)
        finally:
            store.close()

    def test_images_attach_only_latest_generated_artifact(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        image = artifacts / 'screen.png'
        image.write_bytes(b'test-image-bytes')
        provider = OpenRouter(DEFAULT_MODEL, key='test', state_path=self.root)
        provider.persist = lambda: None
        requests = []
        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}')
        messages = [{'role': 'user', 'content': 'old', '_image_path': str(image)},
                    {'role': 'user', 'content': 'latest', '_image_path': str(image)}]
        with patch('urllib.request.urlopen', response):
            provider.complete(messages, [])
            self.assertEqual(requests[0]['max_tokens'], 16384)
            self.assertEqual(requests[0]['reasoning'], {'effort': 'low'})
            self.assertEqual(requests[0]['messages'][0]['content'], 'old')
            self.assertEqual(requests[0]['messages'][1]['content'][1]['type'], 'image_url')
            self.assertNotIn('_image_path', json.dumps(requests))
            messages[-1]['_image_path'] = str(self.root / 'outside.png')
            with self.assertRaisesRegex(ValueError, 'generated run artifact'):
                provider.complete(messages, [])

    def test_sigterm_closes_relay_without_changing_chat_state(self):
        store = initialize(self.app.root / 'chat-stop', self.root, 'Task', scope='filesystem', ui=True)
        store.update_agent('coordinator', state='awaiting_codex')
        store.close()
        script = 'import sys; from conductor.ui_server import serve; serve(root=sys.argv[1])'
        process = subprocess.Popen([sys.executable, '-c', script, str(self.app.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('url', json.loads(process.stdout.readline()))
            process.terminate()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            store = Store(self.app.root / 'chat-stop')
            try:
                self.assertEqual(store.agent('coordinator')['state'], 'awaiting_codex')
            finally:
                store.close()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
