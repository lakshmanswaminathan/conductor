import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from conductor.cli import initialize
from conductor.runtime import AgentLoop
from conductor.swarm_runtime import context_index
from conductor.ui_server import App, make_server


class SwarmDesktopTest(unittest.TestCase):
    def test_dashboard_and_coordinator_share_durable_state_with_permission_gate(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            app = App(root / 'runs')
            server = make_server(app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(path, body=None, authenticated=True):
                headers = {'Content-Type': 'application/json'}
                if authenticated:
                    headers['X-Conductor-Token'] = app.token
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}' + path,
                                             data=json.dumps(body).encode() if body is not None else None,
                                             headers=headers)
                with urllib.request.urlopen(req) as response:
                    return json.load(response)
            store = initialize(app.root / 'test', root, 'Coordinate peers', scope='filesystem', ui=True)
            try:
                store.configure(swarm_root=str(root / 'swarm'), computer_access=True)
                loop = AgentLoop(store, None, 'coordinator')
                self.assertIn('swarm_task', loop.allowed)
                task = {'action': 'create', 'task_id': 'review', 'title': 'Review result',
                        'description': 'Check evidence before accepting', 'request_id': 'create-review'}
                result = request('/api/swarm/action', {'name': 'swarm_task', 'arguments': task})
                self.assertEqual(result['task_id'], 'review')
                self.assertEqual(loop.execute('swarm_task', {'action': 'list'})['tasks'][0]['task_id'], 'review')
                loop.execute('swarm_publish', {'summary': 'Verified result', 'sources': ['session-a:event-1'], 'request_id': 'finding-one'})
                snapshot = request('/api/swarm')
                self.assertEqual(snapshot['findings'][0]['actor'], 'conductor')
                self.assertEqual(snapshot['findings'][0]['sources'], ['session-a:event-1'])
                self.assertNotIn('bootstrap_token', json.dumps(snapshot))
                self.assertLessEqual(len(json.dumps(context_index(root / 'swarm', 900)).encode()), 900)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/swarm', authenticated=False)
                self.assertEqual(error.exception.code, 401); error.exception.close()
                app.update_settings({'computer_access': False})
                self.assertFalse(request('/api/swarm')['service']['enabled'])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/swarm/action', {'name': 'swarm_task', 'arguments': task})
                self.assertEqual(error.exception.code, 400); error.exception.close()
                with self.assertRaisesRegex(RuntimeError, 'disabled'):
                    loop.execute('swarm_task', {'action': 'list'})
            finally:
                store.close(); server.shutdown(); server.server_close(); thread.join(); app.close()


if __name__ == '__main__':
    unittest.main()
