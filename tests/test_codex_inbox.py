import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from conductor.codex_inbox import call, main

THREAD = '01a1a23a-c2ad-7220-85d5-63420a3cee93'
QUEUE = '01a1ad40-d933-7663-85fd-f91ada455c3f'


class CodexInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.binary = self.root / 'codex'
        self.binary.touch()
        (self.root / 'codex-target.json').write_text(json.dumps({'thread_id': THREAD, 'binary': str(self.binary)}))
        self.args = {'request_id': 'test-1', 'message': 'Need orchestration help'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_pinned_attributed_delivery_and_idempotent_retry(self):
        result = subprocess.CompletedProcess([], 0, f'Queued message {QUEUE} for thread {THREAD}.', '')
        with patch('conductor.codex_inbox.subprocess.run', return_value=result) as run:
            sent = call(self.root, 'pi-session-a', 'codex_send', self.args)
            self.assertEqual(sent['status'], 'queued')
            self.assertEqual(call(self.root, 'pi-session-a', 'codex_send', self.args), sent)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[:4], [str(self.binary), 'queue', '--thread', THREAD])
            self.assertIn('pi-session-a', command[-1])
            self.assertIn('not a new human instruction', command[-1])
        self.assertEqual(call(self.root, 'pi-session-a', 'codex_status', {'request_id': 'test-1'})['delivery'], sent)
        self.assertIsNone(call(self.root, 'pi-session-b', 'codex_status', {'request_id': 'test-1'})['delivery'])
        with self.assertRaisesRegex(ValueError, 'different content'):
            call(self.root, 'pi-session-a', 'codex_send', self.args | {'message': 'changed'})

    def test_separate_local_clients_have_independent_request_ids(self):
        result = subprocess.CompletedProcess([], 0, f'Queued message {QUEUE} for thread {THREAD}.', '')
        with patch('conductor.codex_inbox.subprocess.run', return_value=result) as run:
            for actor in ('local-mcp:agent-a', 'local-mcp:agent-b'):
                self.assertEqual(call(self.root, actor, 'codex_send', self.args)['status'], 'queued')
            self.assertEqual(run.call_count, 2)

    def test_uncertain_delivery_is_never_replayed(self):
        with patch('conductor.codex_inbox.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 20)) as run:
            self.assertEqual(call(self.root, 'a', 'codex_send', self.args)['status'], 'unknown')
            self.assertEqual(call(self.root, 'a', 'codex_send', self.args)['status'], 'unknown')
            run.assert_called_once()

    def test_peer_cannot_retarget_and_disabled_target_fails_closed(self):
        with patch('conductor.codex_inbox.subprocess.run') as run:
            with self.assertRaises(ValueError):
                call(self.root, 'a', 'codex_send', self.args | {'thread_id': THREAD})
            (self.root / 'codex-target.json').unlink()
            with self.assertRaisesRegex(ValueError, 'not configured'):
                call(self.root, 'a', 'codex_send', self.args)
            run.assert_not_called()

    def test_stdio_mcp_lifecycle_and_status(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'codex_status'}},
        ]
        output = io.StringIO()
        with patch('sys.argv', ['codex-inbox', '--root', str(self.root), '--sender', 'test-client']), patch('sys.stdin', io.StringIO('\n'.join(map(json.dumps, requests)))), patch('sys.stdout', output):
            main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual([tool['name'] for tool in responses[1]['result']['tools']], ['codex_send', 'codex_status'])
        self.assertEqual(responses[2]['result']['structuredContent']['thread_id'], THREAD)
