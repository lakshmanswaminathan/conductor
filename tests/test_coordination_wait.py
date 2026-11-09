import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from conductor.cli import initialize
from conductor.coordination_wait import progress, wait_for_progress
from conductor.orchestration import begin_scope
from conductor.swarm import SwarmBroker


class CoordinationWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = initialize(root/'run', root, 'Coordinate', ui=True, scope='filesystem')
        self.store.configure(swarm_root=str(root/'swarm'))
        begin_scope(self.store)
        self.store.add_agent('builder', 'coordinator', 'Build', [])
        self.store.configure(**{'assignment:builder': {'kind': 'pi', 'scope': self.store.config()['orchestration_scope'], 'session_id': 'pi-one'}})
        self.store.update_agent('builder', state='running')
        self.store.update_agent('coordinator', state='running')
        self.scheduler = Mock()
        self.broker = SwarmBroker(root/'swarm', Mock())
        self.now = 0

    def tearDown(self):
        self.broker.close()
        self.store.close()
        self.tmp.cleanup()

    def wait(self, on_sleep=lambda: None, **args):
        def sleep(seconds):
            self.now += seconds
            on_sleep()
        with patch('conductor.coordination_wait.time.monotonic', side_effect=lambda: self.now), \
             patch('conductor.coordination_wait.time.sleep', side_effect=sleep):
            return wait_for_progress(self.store, self.scheduler, args)

    def test_default_wait_ignores_heartbeats_and_observation_noise_for_sixty_seconds(self):
        def noise():
            self.store.update_agent('builder', heartbeat=self.now)
            self.store.event('pi', '*', 'observation', 'same leaf')
        result = self.wait(noise)
        self.assertEqual(result['reason'], 'timeout')
        self.assertFalse(result['changed'])
        self.assertEqual(result['waited_seconds'], 60)

    def test_user_input_and_cancellation_wake_promptly(self):
        result = self.wait(lambda: self.store.event('user', '*', 'instruction', 'New constraint'))
        self.assertEqual(result['reason'], 'progress')
        self.assertEqual(result['waited_seconds'], .5)
        result = self.wait(lambda: self.store.configure(cancel_requested=True))
        self.assertEqual(result['reason'], 'cancelled')

    def test_peer_message_finding_and_result_wake_without_status_change(self):
        result = self.wait(lambda: self.broker.call('pi-one', 'swarm_send', {'to': 'conductor', 'message': 'Checkpoint available', 'request_id': 'checkpoint'}))
        self.assertEqual(result['reason'], 'progress')
        result = self.wait(lambda: self.broker.call('pi-one', 'swarm_publish', {'summary': 'Evidence found', 'sources': ['file-a'], 'request_id': 'finding'}))
        self.assertEqual(result['reason'], 'progress')
        result = self.wait(lambda: self.store.update_agent('builder', result='New explicit result'))
        self.assertTrue(result['changed'])

    def test_cursor_catches_progress_before_wait_and_unrelated_peers_do_not_wake(self):
        previous = progress(self.store)['cursor']
        self.store.event('user', '*', 'instruction', 'Continue')
        result = self.wait(after_cursor=previous)
        self.assertEqual(result['reason'], 'progress')
        self.assertEqual(result['waited_seconds'], 0)
        self.broker.call('other-pi', 'swarm_send', {'to': 'conductor', 'message': 'Other chat', 'request_id': 'other'})
        previous = progress(self.store)['cursor']
        def unrelated():
            self.broker.call('other-pi', 'swarm_publish', {'summary': 'Other task finding', 'sources': [], 'request_id': 'unrelated'})
        result = self.wait(unrelated, after_cursor=previous, timeout_seconds=2)
        self.assertEqual(result['reason'], 'timeout')
        self.assertEqual(result['waited_seconds'], 2)

    def test_ready_review_no_workers_and_invalid_waits(self):
        self.store.update_agent('builder', state='review')
        self.assertEqual(self.wait()['reason'], 'review_ready')
        self.store.update_agent('builder', state='done')
        self.assertEqual(self.wait()['reason'], 'no_active_workers')
        for args in ({'timeout_seconds':0}, {'timeout_seconds':61}, {'timeout_seconds':True}, {'after_cursor':'bad'}):
            with self.assertRaises(ValueError):
                self.wait(**args)
