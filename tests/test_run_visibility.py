import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from conductor.cli import initialize
from conductor.ui_server import App


class RunVisibilityTest(unittest.TestCase):
    def test_snapshot_settings_and_trusted_feedback_ingress(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory) / 'runs')
            app.key = 'fixture'
            try:
                for value in (-1, float('nan'), float('inf'), True):
                    with self.assertRaises(ValueError):
                        app.validate_settings({'max_spend': value})
                self.assertEqual(app.validate_settings({'max_spend': 0, 'pi_compaction_tokens': 0}), {'max_spend': 0, 'pi_compaction_tokens': 0})
                with patch.object(app, 'start'):
                    result = app.create({'message': 'Delegated task', 'request_id': 'fixture'}, feedback={'task_key': 'task-1', 'revision': 2})
                from conductor.store import Store
                store = Store(app.path(result['id']))
                try:
                    self.assertEqual(store.user_context(), [])
                    self.assertEqual(store.feedback_context()[0]['body'], 'Delegated task')
                    store.configure(api_calls=7, usage={'cost': 1.25}, coordinator_wait={'status': 'waiting', 'review_at': 200})
                    store.event('harness', 'coordinator', 'compaction', json.dumps({'before_bytes': 2000, 'after_bytes': 1000}))
                    Path(store.root, 'requests.json').write_text(json.dumps([{'status': 'outcome_unknown', 'cost': None}]))
                    snapshot = app.snapshot(result['id'])
                    self.assertEqual(snapshot['api_calls'], 7)
                    self.assertEqual(snapshot['usage']['cost'], 1.25)
                    self.assertEqual(snapshot['requests']['unknown_charges'], 1)
                    self.assertEqual(snapshot['wait']['review_at'], 200)
                    self.assertEqual(len(snapshot['compaction']), 1)
                    self.assertIsNotNone(snapshot['last_progress_at'])
                finally:
                    store.close()
            finally:
                app.close()

    def test_blocked_guard_stops_outer_app_loop(self):
        import threading
        from conductor.runtime import AgentLoop
        from conductor.store import Store
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory) / 'runs', provider_factory=lambda store, key: object())
            app.key = 'fixture'
            reached = threading.Event()
            invocations = []
            try:
                store = initialize(app.root / 'chat-guard', Path(directory), 'Human goal', scope='filesystem', ui=True)
                store.add_feedback('Delegated update', 'feedback-1')
                store.close()
                def run_guard(store, provider):
                    invocations.append(1)
                    if len(invocations) > 1:
                        raise AssertionError('Blocked guard was restarted without new input')
                    loop = AgentLoop(store, provider, 'coordinator')
                    calls = [{'function': {'name': 'pi_control', 'arguments': '{"session_id":"fixture"}'}}]
                    with patch('conductor.coordination_wait.progress', return_value={'cursor': 'unchanged', 'agents': []}):
                        for _ in range(3):
                            stopped = loop.avoid_repetition(calls, [{'error': 'same failure'}], 'unchanged')
                    self.assertTrue(stopped)
                    reached.set()
                with patch('conductor.ui_server.run_coordinator', side_effect=run_guard), patch.object(app, '_report_outcome'):
                    app.start('chat-guard')
                    self.assertTrue(reached.wait(3), 'Guard did not finish')
                    with app.lock:
                        worker = app.jobs.get('chat-guard')
                    if worker:
                        worker.join(3)
                        self.assertFalse(worker.is_alive())
                    self.assertFalse(app.is_running('chat-guard'))
                    self.assertEqual(len(invocations), 1)
                store = Store(app.path('chat-guard'))
                try:
                    self.assertEqual(store.agent('coordinator')['state'], 'blocked')
                    self.assertEqual(store.config()['processed_user_version'], 1)
                    self.assertEqual(store.config()['processed_feedback_version'], store.feedback_version())
                finally:
                    store.close()
            finally:
                app.close()

    def test_delegated_goal_survives_feedback_flood(self):
        from conductor.context import Context
        from conductor.store import Store
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory) / 'runs')
            app.key = 'fixture'
            try:
                with patch.object(app, 'start'):
                    result = app.create({'message': 'Original delegated goal', 'request_id': 'goal'}, feedback={'task_key': 'task-1'})
                store = Store(app.path(result['id']))
                try:
                    store.configure(computer_access=False)
                    for i in range(40):
                        store.add_feedback('Later feedback ' + str(i) + 'x' * 1000, 'feedback-' + str(i))
                    messages = Context(store, None, budget=24000).build('coordinator', 'Fixture system')
                    shared = json.loads(messages[1]['content'].split('\n', 1)[1])
                    self.assertEqual(shared['delegated_goal']['body'], 'Original delegated goal')
                    self.assertEqual(shared['user_instructions'], [])
                    self.assertGreater(shared['omitted_messages'], 0)
                    self.assertEqual(len(store.feedback_context()), 41)
                finally:
                    store.close()
            finally:
                app.close()
