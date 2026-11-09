import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conductor.swarm_notifications import send
from conductor.swarm_runtime import codex_tools


class NotificationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SimpleNamespace(root=Path(self.temp.name))
        self.target = patch('conductor.codex_inbox.target', return_value=('target-one', '/mock/codex')).start()
        self.addCleanup(patch.stopall)
        self.calls = []

    def transport(self, root, actor, name, args):
        self.calls.append(args)
        return {'request_id': args['request_id'], 'thread_id': 'target-one', 'status': 'queued', 'queue_id': 'queue'}

    def send(self, request='one', message='report', **metadata):
        return send(self.store, '/mock', 'actor', {'request_id': request, 'message': message, **metadata}, self.transport)

    def test_legacy_exact_content_and_conflicts(self):
        self.send()
        duplicate = self.send('two')
        self.assertTrue(duplicate['deduplicated'])
        self.assertEqual(duplicate['request_id'], 'one')
        self.assertIn('accepted', duplicate['semantics'])
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.send('two', 'changed')
        self.send('three', 'substantive new report')
        self.assertEqual(len(self.calls), 2)

    def test_checkpoint_collision_requires_new_cursor(self):
        meta = dict(notification_kind='progress', task_key='task', checkpoint='review', progress_cursor='1')
        self.send(**meta)
        self.assertTrue(self.send('two', **meta)['deduplicated'])
        with self.assertRaisesRegex(ValueError, 'advance checkpoint'):
            self.send('three', 'new evidence', **meta)
        self.send('three', 'new evidence', **{**meta, 'progress_cursor': '2'})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(set(self.calls[0]), {'message', 'request_id'})

    def test_preserves_failure_escalation_and_final(self):
        self.send(notification_kind='progress', task_key='task', checkpoint='c')
        for kind in ('failure', 'escalation', 'final'):
            self.send(kind, notification_kind=kind, task_key='task')
        self.assertEqual(len(self.calls), 4)

    def test_ambiguous_delivery_is_not_replayed(self):
        def interrupted(*args):
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            send(self.store, '/mock', 'actor', {'request_id': 'one', 'message': 'report'}, interrupted)
        self.assertEqual(self.send('two')['status'], 'unknown')
        self.assertFalse(self.calls)

    def test_transport_conflict_remains_an_error(self):
        def conflict(*args):
            raise ValueError('request_id already belongs to different content or target')
        with self.assertRaisesRegex(ValueError, 'different content'):
            send(self.store, '/mock', 'actor', {'request_id': 'one', 'message': 'report'}, conflict)
        with self.assertRaisesRegex(ValueError, 'different content'):
            send(self.store, '/mock', 'actor', {'request_id': 'one', 'message': 'report'}, conflict)
        self.assertFalse(self.calls)

    def test_rate_limit_retries_preserve_fingerprint(self):
        def limited(*args):
            raise ValueError('Codex inbox rate limit: 10 messages per minute')
        with self.assertRaisesRegex(ValueError, 'rate limit'):
            send(self.store, '/mock', 'actor', {'request_id': 'one', 'message': 'report'}, limited)
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.send(message='changed')
        self.assertEqual(self.send()['status'], 'queued')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.send('two')['deduplicated'])

    def test_scopes_target_and_manager_chat(self):
        self.send()
        self.target.return_value = ('target-two', '/mock/codex')
        self.send('two')
        with self.assertRaisesRegex(ValueError, 'different content or target'):
            self.send()
        other = self.store.root / 'other'
        other.mkdir()
        self.store = SimpleNamespace(root=other)
        self.send()
        self.assertEqual(len(self.calls), 3)

    def test_validation_and_model_schema(self):
        with self.assertRaisesRegex(ValueError, 'task_key and checkpoint'):
            self.send(notification_kind='progress')
        with self.assertRaisesRegex(ValueError, 'Unknown inbox argument'):
            self.send(extra=True)
        schema = next(t['function']['parameters'] for t in codex_tools() if t['function']['name'] == 'codex_send')
        self.assertIn('checkpoint', schema['properties'])
        from conductor.codex_inbox import TOOLS
        self.assertNotIn('checkpoint', TOOLS[0]['inputSchema']['properties'])

    def test_new_progress_cursor_preserves_same_text_at_new_milestone(self):
        meta = dict(notification_kind='progress', task_key='task', checkpoint='review')
        self.send(progress_cursor='1', **meta)
        self.send('two', progress_cursor='2', **meta)
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
