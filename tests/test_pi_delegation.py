import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conductor.cli import initialize
from conductor.pi_delegation import delegate, tick
from conductor.swarm import SwarmBroker


class Monitor:
    def __init__(self):
        self.instance = 'instance-one'
        self.status = 'idle'
        self.outcome = 'accepted'
        self.calls = []

    def bridges(self):
        return {'peer': {'instance_id': self.instance, 'status': self.status}} if self.instance else {}

    def command(self, *args):
        self.calls.append(args)
        return {'status': self.outcome, 'delivery': 'queued' if self.status == 'working' else 'started'}


class PiDelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = initialize(self.root / 'run', self.root, 'Coordinate', demo=True, transport='process')
        self.store.configure(computer_access=True, swarm_root=str(self.root / 'swarm'), orchestration_scope='goal-one')
        self.monitor = Monitor()
        self.patch = patch('conductor.pi_delegation.monitor', self.monitor)
        self.patch.start()
        self.args = {'id': 'worker', 'task': 'Inspect assigned module', 'ownership': ['module.py'],
                     'acceptance': ['Report evidence'], 'dependencies': [],
                     'session_id': 'peer', 'instance_id': 'instance-one'}

    def tearDown(self):
        self.patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def broker(self):
        return SwarmBroker(self.root / 'swarm', self.monitor)

    def test_idle_delivery_is_running_not_done_and_duplicate_is_idempotent(self):
        first = delegate(self.store, self.args)
        second = delegate(self.store, self.args)
        self.assertEqual(first, second)
        self.assertEqual(first['state'], 'running')
        self.assertEqual(len(self.monitor.calls), 1)
        self.assertEqual(self.monitor.calls[0][3], 'follow_up')
        self.assertIn(first['swarm_task_id'], self.monitor.calls[0][4])
        assignment = self.store.config()['assignment:worker']
        self.assertEqual(assignment['scope'], 'goal-one')
        self.assertTrue(assignment['review_required'])
        with self.assertRaisesRegex(ValueError, 'different assignment'):
            delegate(self.store, self.args | {'task': 'Different work'})

    def test_busy_peer_queues_follow_up(self):
        self.monitor.status = 'working'
        result = delegate(self.store, self.args)
        self.assertEqual(result['delivery']['delivery'], 'queued')
        self.assertEqual(self.monitor.calls[0][3], 'follow_up')

    def test_dependencies_wait_for_local_acceptance(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Prerequisite')
        self.store.update_agent('prerequisite', state='review')
        result = delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(self.monitor.calls)
        self.store.update_agent('prerequisite', state='done')
        tick(self.store)
        self.assertEqual(len(self.monitor.calls), 1)
        broker = self.broker()
        try:
            task = broker.call('conductor', 'swarm_task', {'action': 'list'})['tasks'][0]
            self.assertEqual(task['dependencies'], [])
            self.assertEqual(task['owner'], 'peer')
        finally:
            broker.close()

    def test_unknown_receipt_never_replays(self):
        self.monitor.outcome = 'unknown'
        self.assertEqual(delegate(self.store, self.args)['state'], 'blocked')
        self.monitor.outcome = 'accepted'
        tick(self.store)
        delegate(self.store, self.args)
        self.assertEqual(len(self.monitor.calls), 1)
        self.assertIn('No automatic replay', self.store.agent('worker')['result'])

    def test_wrong_instance_rejected_and_replacement_blocks(self):
        with self.assertRaisesRegex(ValueError, 'Exact live'):
            delegate(self.store, self.args | {'instance_id': 'other'})
        self.assertIsNone(self.store.agent('worker'))
        delegate(self.store, self.args)
        self.monitor.instance = 'replacement'
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'blocked')
        self.assertEqual(len(self.monitor.calls), 1)

    def test_reported_completion_requires_review_and_keeps_evidence(self):
        result = delegate(self.store, self.args)
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': result['swarm_task_id'],
                        'status': 'completed', 'result': 'Verified tests: evidence-42', 'request_id': 'report'})
        finally:
            broker.close()
        tick(self.store)
        agent = self.store.agent('worker')
        self.assertEqual(agent['state'], 'review')
        self.assertEqual(json.loads(agent['result'])['reported_result'], 'Verified tests: evidence-42')
        tick(self.store)
        self.assertEqual(len(self.monitor.calls), 1)

    def test_crash_during_send_is_not_replayed(self):
        delegate(self.store, self.args)
        assignment = self.store.config()['assignment:worker'] | {'phase': 'sending'}
        self.store.configure(**{'assignment:worker': assignment})
        self.store.update_agent('worker', state='pending')
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'blocked')
        self.assertEqual(len(self.monitor.calls), 1)

    def test_disabled_or_cancelled_ticks_do_not_dispatch(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Prerequisite')
        delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        self.store.update_agent('prerequisite', state='done')
        self.store.configure(computer_access=False)
        tick(self.store)
        self.store.configure(computer_access=True, cancel_requested=True)
        tick(self.store)
        self.assertFalse(self.monitor.calls)

    def test_peer_failure_blocks_local_task(self):
        result = delegate(self.store, self.args)
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': result['swarm_task_id'],
                        'status': 'failed', 'result': 'Verification failed', 'request_id': 'failed'})
        finally:
            broker.close()
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'blocked')

    def test_old_scope_pending_assignment_does_not_launch_after_dependency_finishes(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Prerequisite')
        delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        self.store.configure(orchestration_scope='goal-two')
        self.store.update_agent('prerequisite', state='done')
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'pending')
        self.assertFalse(self.monitor.calls)

    def test_superseded_assignment_is_not_launched(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Prerequisite')
        delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        spec = self.store.config()['assignment:worker'] | {'superseded_by': 'replacement'}
        self.store.configure(**{'assignment:worker': spec})
        self.store.update_agent('prerequisite', state='done')
        tick(self.store)
        self.assertFalse(self.monitor.calls)

    def test_capacity_counts_current_scope_only(self):
        self.store.configure(max_tasks=1)
        delegate(self.store, self.args)
        self.store.configure(orchestration_scope='goal-two')
        result = delegate(self.store, self.args | {'id': 'next-worker'})
        self.assertEqual(result['state'], 'running')
        self.assertEqual(len(self.monitor.calls), 2)
        with self.assertRaisesRegex(ValueError, 'ceiling'):
            delegate(self.store, self.args | {'id': 'over-capacity'})


if __name__ == '__main__':
    unittest.main()
