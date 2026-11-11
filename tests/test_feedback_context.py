import json
import tempfile
import unittest
from pathlib import Path

from conductor.context import Context
from conductor.store import Store
from conductor import conductor_mailbox


class FeedbackContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'runs' / 'chat')
        self.store.add_agent('coordinator', None, 'Preserve the authorized goal')
        self.store.event('user', '*', 'instruction', 'Must preserve scope; never deploy without approval')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_flood_is_bounded_retrievable_and_not_human_authority(self):
        for n in range(422):
            self.store.add_feedback(f'Feedback {n}: ' + 'evidence ' * 80, f'request-{n}', 'task', str(n))
        snapshot = json.loads(Context(self.store, None).fixed('coordinator', 'system')[1]['content'].split('\n', 1)[1])
        self.assertEqual(len(snapshot['user_instructions']), 1)
        self.assertIn('never deploy', snapshot['user_instructions'][0]['body'])
        self.assertGreater(snapshot['omitted_messages'], 400)
        self.assertEqual(len(self.store.inbox('coordinator')), 422)
        self.assertEqual(len(self.store.feedback_context()), 422)
        self.assertIn('search_context', snapshot['retrieval'])
        version = self.store.feedback_version()
        self.store.add_feedback('Feedback 421: ' + 'evidence ' * 80, 'request-421', 'task', '421')
        self.assertEqual(self.store.feedback_version(), version)
        with self.assertRaises(ValueError):
            self.store.add_feedback('different', 'request-421')
        self.assertEqual(len(self.store.user_context()), 1)
        self.store.deliver_user_instructions('coordinator')
        self.assertEqual(len(self.store.history('coordinator')), 1)
        self.store.event('user', '*', 'instruction', 'New human constraint')
        self.assertEqual(len(self.store.user_context()), 2)
        self.assertEqual(self.store.feedback_version(), version)

    def test_legacy_requires_trusted_mailbox_provenance(self):
        conductor_mailbox.enqueue(self.root, {'request_id': 'legacy', 'message': 'Agent report'})
        body = conductor_mailbox.pending(self.root)[0]['message']
        self.store.event('user', '*', 'instruction', body)
        self.assertEqual(len(self.store.user_context()), 1)
        self.assertEqual(len(self.store.feedback_context()), 1)
        self.store.event('user', '*', 'instruction', body.replace('legacy]', 'untrusted]'))
        self.assertEqual(len(self.store.user_context()), 2)
        self.assertEqual(len(self.store.instruction_events()), 3)
        self.assertEqual(len(self.store.inbox('coordinator')), 1)

    def test_initial_legacy_goal_is_preserved(self):
        other = Store(self.root / 'runs' / 'other')
        try:
            conductor_mailbox.enqueue(self.root, {'request_id': 'initial', 'message': 'Original authorized goal'})
            body = conductor_mailbox.pending(self.root)[0]['message']
            other.event('user', '*', 'instruction', body)
            self.assertEqual(other.user_context()[0]['body'], body)
            self.assertEqual(other.feedback_context(), [])
        finally:
            other.close()

    def test_mailbox_metadata_and_feedback_ack(self):
        args = {'request_id': 'new', 'chat_id': 'chat', 'message': 'evidence', 'task_key': 'task', 'revision': 3}
        conductor_mailbox.enqueue(self.root, args)
        item = conductor_mailbox.pending(self.root)[0]
        self.assertEqual(item['message_kind'], 'agent_feedback')
        self.store.add_feedback(item['message'], item['request_id'], item['task_key'], item['revision'])
        conductor_mailbox.ack(self.root, 'new', 'chat')
        self.assertEqual(conductor_mailbox.result(self.root, 'new')['status'], 'delivered')
        self.assertEqual(len(self.store.user_context()), 1)
        with self.assertRaises(ValueError):
            conductor_mailbox.enqueue(self.root, args | {'revision': 4})

    def test_compaction_attributes_usage_and_preserves_executor_capabilities(self):
        for _ in range(12):
            self.store.append('coordinator', {'role': 'assistant', 'content': 'Evidence ' * 150})
        calls = []
        class Summary:
            def complete(inner, messages, tools, max_tokens, purpose='execution', agent=None):
                calls.append((messages, tools, purpose, agent))
                return {'content': 'Evidence retained in original event archive.'}
        tool = {'type': 'function', 'function': {'name': 'pi_dispatch', 'parameters': {}}}
        result = Context(self.store, Summary(), 6000).build('coordinator', 'system', [tool])
        self.assertIn('pi_dispatch', result[0]['content'])
        self.assertTrue(calls)
        for messages, tools, purpose, agent in calls:
            self.assertEqual((purpose, agent), ('compaction', 'coordinator'))
            self.assertEqual(tools, [])
            self.assertEqual(json.loads(messages[1]['content'])['executor_tools'], ['pi_dispatch'])
        events = list(self.store.db.execute("SELECT body FROM events WHERE kind='compaction'"))
        self.assertEqual(len(events), len(calls))
        for row in events:
            value = json.loads(row['body'])
            self.assertGreater(value['before_bytes'], value['after_bytes'])

    def test_resolution_archives_explicit_feedback_and_preserves_raw_retrieval(self):
        self.store.add_feedback('Old feedback', 'old', 'task', '1')
        self.store.add_feedback('New feedback', 'new', 'task', '2')
        ingress_version = self.store.feedback_version()
        record = self.store.resolve_feedback('old', ['report:accepted'], 'superseded')
        self.assertEqual(self.store.resolve_feedback('old', ['report:accepted'], 'superseded'), record)
        self.assertEqual(self.store.feedback_version(), ingress_version)
        self.assertEqual([row['request_id'] for row in self.store.active_inbox('coordinator')], ['new'])
        self.assertEqual(len(self.store.feedback_context()), 2)
        self.assertEqual(len(self.store.inbox('coordinator')), 3)
        snapshot = json.loads(Context(self.store, None).fixed('coordinator', 'system')[1]['content'].split('\n', 1)[1])
        self.assertEqual(snapshot['archived_feedback'], 1)
        self.assertEqual(snapshot['omitted_messages'], 0)
        self.assertEqual([row['request_id'] for row in snapshot['messages']], ['new'])
        event = self.store.db.execute('SELECT * FROM events WHERE id=?', (record['event_id'],)).fetchone()
        self.assertEqual(event['kind'], 'feedback_resolution')
        self.assertEqual(json.loads(event['body'])['evidence'], ['report:accepted'])
        with self.assertRaisesRegex(ValueError, 'different resolution'):
            self.store.resolve_feedback('old', ['changed'])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.store.resolve_feedback('missing', ['report'])
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.store.resolve_feedback('new', [])
        self.assertEqual(len(self.store.active_inbox('coordinator')), 1)

    def test_delegated_goal_remains_pinned_across_feedback_flood(self):
        goal = 'Original delegated goal: produce evidence, preserve approval constraints'
        self.store.configure(delegated_goal=goal)
        for n in range(422):
            self.store.add_feedback('feedback ' * 100, f'feedback-{n}', 'task', str(n))
        snapshot = json.loads(Context(self.store, None).fixed('coordinator', 'system')[1]['content'].split('\n', 1)[1])
        self.assertEqual(snapshot['delegated_goal'], goal)
        self.assertGreater(snapshot['omitted_messages'], 400)
        self.assertEqual(len(snapshot['user_instructions']), 1)
