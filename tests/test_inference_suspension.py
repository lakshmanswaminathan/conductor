import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from conductor.cli import initialize
from conductor.coordination_wait import progress, suspend_for_progress
from conductor.runtime import AgentLoop


class InferenceSuspensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = initialize(root/'run', root, 'Coordinate fixture', ui=True, context_budget=40000)
        self.store.add_agent('worker', 'coordinator', 'Fixture work')
        self.store.update_agent('worker', state='running')
        self.store.update_agent('coordinator', state='running')
        self.clock = 0
        self.scheduler = Mock()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def drive(self, function, on_sleep):
        def sleep(seconds):
            self.clock += seconds
            on_sleep()
        with patch('conductor.coordination_wait.time.monotonic', side_effect=lambda: self.clock), \
             patch('conductor.coordination_wait.time.time', side_effect=lambda: 1000 + self.clock), \
             patch('conductor.coordination_wait.time.sleep', side_effect=sleep):
            return function()

    def test_multiple_timeout_chunks_do_not_call_model(self):
        class Provider:
            calls = 0
            def complete(inner, messages, tools):
                inner.calls += 1
                name, args = ('wait', {}) if inner.calls == 1 else ('finish', {'summary':'verified', 'findings':[], 'outcome':'done'})
                return {'tool_calls':[{'id':str(inner.calls), 'function':{'name':name, 'arguments':json.dumps(args)}}]}
        provider = Provider()
        loop = AgentLoop(self.store, provider, 'coordinator', self.scheduler)
        self.drive(loop.run, lambda: self.store.update_agent('worker', state='done') if self.clock >= 121 else None)
        self.assertEqual(provider.calls, 2)
        self.assertGreaterEqual(self.clock, 121)
        self.assertEqual(self.store.agent('coordinator')['state'], 'done')

    def test_persisted_wait_resumes_before_inference_and_feedback_wakes_it(self):
        self.store.configure(coordinator_wait={'status':'waiting', 'cursor':progress(self.store)['cursor'], 'started_at':1000, 'review_at':None})
        version = len(self.store.user_context())
        result = self.drive(lambda: suspend_for_progress(self.store, self.scheduler, resume=True),
                            lambda: self.store.add_feedback('Fresh result', 'feedback-one'))
        self.assertEqual(result['reason'], 'progress')
        self.assertEqual(len(self.store.user_context()), version)
        self.assertEqual(self.store.config()['coordinator_wait']['status'], 'resumed')

    def test_explicit_deadline_and_cancel_exit_without_inference(self):
        result = self.drive(lambda: suspend_for_progress(self.store, self.scheduler, {'review_after_seconds':2}), lambda: None)
        self.assertEqual(result['reason'], 'review_deadline')
        result = self.drive(lambda: suspend_for_progress(self.store, self.scheduler), lambda: self.store.configure(cancel_requested=True))
        self.assertEqual(result['reason'], 'cancelled')

    def test_identical_failures_block_and_mark_consumed_input(self):
        self.store.add_feedback('Inspect existing assignment', 'input')
        loop = AgentLoop(self.store, None, 'coordinator', self.scheduler)
        call = {'function':{'name':'dispatch_pi','arguments':'{}'}}
        cursor = progress(self.store)['cursor']
        for _ in range(2):
            self.assertFalse(loop.avoid_repetition([call], [{'error':'Invalid task ID'}], cursor))
        self.assertTrue(loop.avoid_repetition([call], [{'error':'Invalid task ID'}], cursor))
        config = self.store.config()
        self.assertEqual(self.store.agent('coordinator')['state'], 'blocked')
        self.assertEqual(config['processed_user_version'], len(self.store.user_context()))
        self.assertEqual(config['processed_feedback_version'], self.store.feedback_version())

    def test_invalid_wait_does_not_persist_suspension(self):
        for args in ({'timeout_seconds': 0}, {'after_cursor': 'invalid'}, {'review_after_seconds': -1}):
            with self.assertRaises(ValueError):
                suspend_for_progress(self.store, self.scheduler, args)
            self.assertNotIn('coordinator_wait', self.store.config())
        self.scheduler.tick.assert_not_called()

    def test_new_feedback_resets_identical_failure_count(self):
        loop = AgentLoop(self.store, None, 'coordinator', self.scheduler)
        call = {'function': {'name': 'dispatch_pi', 'arguments': '{}'}}
        cursor = progress(self.store)['cursor']
        for _ in range(2):
            self.assertFalse(loop.avoid_repetition([call], [{'error': 'Unavailable owner'}], cursor))
        self.store.add_feedback('Owner reconnected; retry once', 'retry-owner')
        self.assertFalse(loop.avoid_repetition([call], [{'error': 'Unavailable owner'}], progress(self.store)['cursor']))
        self.assertEqual(self.store.config()['loop_guard']['count'], 1)
