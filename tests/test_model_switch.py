import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from conductor.cli import initialize
from conductor.store import Store
from conductor.ui_server import App, make_server


class ModelSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = App(self.root / 'runs')
        self.app.key = ''
        self.store = initialize(self.app.root / 'chat-test', self.root, 'Original instruction', demo=True)
        self.original = self.store.config()['model']

    def tearDown(self):
        self.store.close()
        self.app.close()
        self.tmp.cleanup()

    def test_desktop_model_changes_without_mutating_history(self):
        history = self.store.history('coordinator')
        instructions = self.store.user_context()
        with patch.object(self.app, 'start') as start:
            self.app.set_model('chat-test', {'model': 'vendor/model-v2'})
            start.assert_not_called()
        self.assertEqual(self.store.config()['model'], 'vendor/model-v2')
        self.assertEqual(self.store.history('coordinator'), history)
        self.assertEqual(self.store.user_context(), instructions)

    def test_invalid_model_does_not_change_configuration(self):
        for model in ('', 'model with spaces', 'x' * 121, None, [], 'model\nextra'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.app.set_model('chat-test', {'model': model})
        self.assertEqual(self.store.config()['model'], self.original)

    def test_running_and_stopping_chat_are_preserved(self):
        before = self.store.agent('coordinator')
        with patch.object(self.app, 'is_running', return_value=True), self.assertRaisesRegex(ValueError, 'Stop this chat'):
            self.app.set_model('chat-test', {'model': 'vendor/next'})
        self.app.stopping.add('chat-test')
        try:
            with self.assertRaisesRegex(ValueError, 'Stop this chat'):
                self.app.set_model('chat-test', {'model': 'vendor/next'})
        finally:
            self.app.stopping.discard('chat-test')
        self.assertEqual(self.store.config()['model'], self.original)
        self.assertEqual(self.store.agent('coordinator'), before)

    def test_new_chat_default_switch_needs_no_key_and_leaves_existing_chat(self):
        self.app.update_settings({'model': 'vendor/default-next'})
        self.assertEqual(json.loads(self.app.settings_path.read_text())['model'], 'vendor/default-next')
        self.assertEqual(self.store.config()['model'], self.original)
        self.assertFalse(self.app.key)

    def test_catalog_is_public_cached_and_excludes_non_chat_outputs(self):
        data = {'data': [
            {'id': 'vendor/z', 'name': 'Zed', 'architecture': {'output_modalities': ['text']}},
            {'id': 'vendor/a', 'name': 'Alpha'},
            {'id': 'vendor/image', 'architecture': {'output_modalities': ['image']}},
            {'id': 'invalid id'},
        ]}
        with patch('conductor.ui_server.urlopen', return_value=io.BytesIO(json.dumps(data).encode())) as fetch:
            result = self.app.models()
            self.assertEqual([m['id'] for m in result['models']], ['vendor/a', 'vendor/z'])
            self.assertEqual(self.app.models(), result)
            fetch.assert_called_once()
            self.assertFalse(fetch.call_args.args[0].has_header('Authorization'))

    def test_catalog_failure_preserves_cached_choices_and_allows_retry(self):
        with patch('conductor.ui_server.urlopen', side_effect=OSError('private details')):
            with self.assertRaisesRegex(ValueError, '^Could not load OpenRouter models. Try again.$'):
                self.app.models()
            self.app.model_catalog = [{'id': 'vendor/a', 'name': 'Alpha'}]
            self.app.model_catalog_at = -1000
            result = self.app.models()
            self.assertEqual(result['models'], self.app.model_catalog)
            self.assertIn('last loaded catalog', result['warning'])

    def test_endpoint_requires_authentication_and_rejects_cross_origin(self):
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/chats/chat-test/model'
        def request(headers):
            return urllib.request.urlopen(urllib.request.Request(url, data=b'{"model":"vendor/http-next"}',
                                          headers={'Content-Type': 'application/json'} | headers))
        try:
            for headers, status in [({}, 401), ({'X-Conductor-Token': self.app.token, 'Origin': 'https://example.invalid'}, 403)]:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    request(headers)
                self.assertEqual(raised.exception.code, status)
                raised.exception.close()
            self.assertEqual(self.store.config()['model'], self.original)
            with request({'X-Conductor-Token': self.app.token}) as response:
                self.assertEqual(json.load(response)['model'], 'vendor/http-next')
            self.assertEqual(self.store.config()['model'], 'vendor/http-next')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
