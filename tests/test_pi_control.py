import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

from conductor.pi_monitor import PiMonitor


class PiControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.monitor = PiMonitor(bridge_root=self.root)
        self.endpoint = self.root / 'test.sock'
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.endpoint))
        os.chmod(self.endpoint, 0o600)
        self.server.listen(2)
        self.server.settimeout(2)
        self.bridge = {'session_id': 'agent-a', 'instance_id': 'instance-a',
            'control_protocol': 'conductor.pi.control', 'control_version': 1, 'control_socket': 'test.sock'}
        self.commands = []

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def respond(self, mismatched=False):
        def work():
            with self.server.accept()[0] as client:
                request = json.loads(client.makefile('rb').readline())
                self.commands.append(request)
                response = {k: request[k] for k in ('request_id', 'session_id', 'instance_id')}
                response.update(status='accepted', delivery='steering')
                if mismatched:
                    response['instance_id'] = 'some-other-instance'
                client.sendall((json.dumps(response) + '\n').encode())
        thread = threading.Thread(target=work)
        thread.start()
        return thread

    def test_exact_address_ack_and_restart_safe_deduplication(self):
        thread = self.respond()
        with patch.object(self.monitor, 'bridges', return_value={'agent-a': self.bridge}):
            result = self.monitor.command('agent-a', 'instance-a', 'request-a', 'steer', 'Use the clarified goal')
        thread.join()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.commands[0]['message'], 'Use the clarified goal')
        restarted = PiMonitor(bridge_root=self.root)
        with patch.object(restarted, 'bridges', side_effect=AssertionError('Must use prior receipt')):
            self.assertEqual(restarted.command('agent-a', 'instance-a', 'request-a', 'steer', 'Use the clarified goal'), result)
        self.assertNotIn('Use the clarified goal', (self.root / 'receipts/request-a.json').read_text())
        mismatch = restarted.command('agent-a', 'instance-a', 'request-a', 'steer', 'Different task')
        self.assertEqual(mismatch['status'], 'rejected')
        self.assertEqual(len(self.commands), 1)

    def test_native_compact_receipt_is_durable_and_observable_without_replay(self):
        thread = self.respond()
        with patch.object(self.monitor, 'bridges', return_value={'agent-a': self.bridge}):
            result = self.monitor.command('agent-a', 'instance-a', 'compact-a', 'compact')
        thread.join()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.commands[0]['action'], 'compact')
        self.assertNotIn('message', self.commands[0])
        restarted = PiMonitor(bridge_root=self.root)
        observed = {'request_id': 'compact-a', 'instance_id': 'instance-a', 'state': 'completed'}
        with patch.object(restarted, 'records', return_value={'agent-a': {'compaction': observed}}), patch.object(restarted, 'bridges', side_effect=AssertionError('Must not replay')):
            receipt = restarted.command('agent-a', 'instance-a', 'compact-a', 'compact')
        self.assertEqual(receipt['compaction']['state'], 'completed')
        self.assertEqual(len(self.commands), 1)

    def test_mismatched_ack_is_unknown_and_never_retargeted(self):
        thread = self.respond(mismatched=True)
        with patch.object(self.monitor, 'bridges', return_value={'agent-a': self.bridge}):
            result = self.monitor.command('agent-a', 'instance-a', 'request-a', 'follow_up', 'Next step')
        thread.join()
        self.assertEqual(result['status'], 'unknown')
        with patch.object(self.monitor, 'bridges', return_value={'agent-a': self.bridge}), patch.object(self.monitor, 'control_path', return_value=None):
            self.assertEqual(self.monitor.command('agent-a', 'instance-a', 'request-a', 'follow_up', 'Next step')['status'], 'unknown')
        changed = self.bridge | {'instance_id': 'instance-b'}
        with patch.object(self.monitor, 'bridges', return_value={'agent-a': changed}):
            result = self.monitor.command('agent-a', 'instance-a', 'request-a', 'follow_up', 'Next step')
            self.assertEqual(result['status'], 'unknown')
            result = self.monitor.command('agent-a', 'instance-b', 'request-a', 'follow_up', 'Next step')
            self.assertEqual(result['status'], 'rejected')
        self.assertEqual(len(self.commands), 1)

    def test_stale_instance_unsafe_endpoint_and_limits_reject_before_send(self):
        with patch.object(self.monitor, 'bridges', return_value={'agent-a': self.bridge}):
            self.assertEqual(self.monitor.command('agent-a', 'stale', 'new-request', 'steer', 'hello')['status'], 'rejected')
            self.assertEqual(self.monitor.control_path(self.bridge | {'control_socket': '../elsewhere.sock'}), None)
            os.chmod(self.endpoint, 0o666)
            self.assertEqual(self.monitor.command('agent-a', 'instance-a', 'new-request', 'steer', 'hello')['status'], 'rejected')
        with self.assertRaises(ValueError):
            self.monitor.command('agent-a', 'instance-a', '../bad', 'steer', 'hello')
        with self.assertRaises(ValueError):
            self.monitor.command('agent-a', 'instance-a', 'new-request', 'steer', 'a' * 12001)
        self.assertEqual(self.commands, [])

    def test_http_steering_requires_auth_and_enabled_computer_access(self):
        from conductor.ui_server import App, make_server
        app = App(self.root / 'runs')
        server = make_server(app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{server.server_port}/api/pi/sessions/agent-a/steer'
        body = json.dumps({'instance_id': 'instance-a', 'request_id': 'request-a', 'mode': 'steer', 'message': 'Clarification'}).encode()
        try:
            with patch('conductor.pi_monitor.monitor.command') as command:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(urllib.request.Request(url, data=body))
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
                app.settings['computer_access'] = False
                headers = {'X-Conductor-Token': app.token, 'Content-Type': 'application/json'}
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers))
                self.assertEqual(raised.exception.code, 400)
                raised.exception.close()
                command.assert_not_called()
                app.settings['computer_access'] = True
                command.return_value = {'status': 'accepted', 'request_id': 'request-a'}
                with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers)) as response:
                    self.assertEqual(json.load(response)['status'], 'accepted')
                command.assert_called_once_with('agent-a', 'instance-a', 'request-a', 'steer', 'Clarification')
        finally:
            server.shutdown(); server.server_close(); worker.join(); app.close()


if __name__ == '__main__':
    unittest.main()
