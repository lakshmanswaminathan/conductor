import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conductor.cli import initialize
from conductor.pi_delegation import delegate, tick
from conductor.swarm import SwarmBroker
from conductor.store import Store


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
        self.store.close()
        self.store = Store(self.root / 'run')
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

    def test_legacy_scope_pending_assignment_launches_after_dependency_finishes(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Prerequisite')
        delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        self.store.configure(orchestration_scope='goal-two')
        self.store.update_agent('prerequisite', state='done')
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'running')
        self.assertEqual(len(self.monitor.calls), 1)

    def test_superseded_assignment_is_not_launched(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Prerequisite')
        delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        spec = self.store.config()['assignment:worker'] | {'superseded_by': 'replacement'}
        self.store.configure(**{'assignment:worker': spec})
        self.store.update_agent('prerequisite', state='done')
        tick(self.store)
        self.assertFalse(self.monitor.calls)

    def test_capacity_counts_unresolved_legacy_assignments(self):
        self.store.configure(max_tasks=1)
        delegate(self.store, self.args)
        self.store.configure(orchestration_scope='goal-two')
        with self.assertRaisesRegex(ValueError, 'ceiling'):
            delegate(self.store, self.args | {'id': 'over-capacity'})
        delegate(self.store, self.args)
        self.assertEqual(len(self.monitor.calls), 1)

    def test_empty_completion_waits_for_explicit_result(self):
        receipt = delegate(self.store, self.args)
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': receipt['swarm_task_id'],
                'status': 'completed', 'request_id': 'empty'})
            tick(self.store)
            self.assertEqual(self.store.agent('worker')['state'], 'blocked')
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': receipt['swarm_task_id'],
                'status': 'completed', 'result': 'Verified evidence-42', 'request_id': 'evidence'})
            tick(self.store)
            self.assertEqual(self.store.agent('worker')['state'], 'review')
        finally:
            broker.close()
        self.assertEqual(len(self.monitor.calls), 1)

    def test_followup_review_releases_pi_dependency(self):
        from conductor.orchestration import begin_scope
        from conductor.runtime import AgentLoop, Scheduler
        first = delegate(self.store, self.args)
        delegate(self.store, self.args | {'id': 'dependent', 'dependencies': ['worker']})
        self.store.event('user', '*', 'instruction', 'Continue and review')
        self.assertEqual(begin_scope(self.store), 'goal-one')
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': first['swarm_task_id'],
                'status': 'completed', 'result': 'Verified evidence-42', 'request_id': 'complete'})
        finally:
            broker.close()
        tick(self.store)
        self.assertEqual(len(self.monitor.calls), 1)
        manager = AgentLoop(self.store, None, 'coordinator', Scheduler(self.store, self.root/'unused.sock'))
        manager.execute('review_task', {'agent': 'worker', 'accepted': True,
            'summary': 'Evidence checked', 'evidence': ['evidence-42']})
        self.assertEqual(self.store.agent('worker')['state'], 'done')
        self.assertEqual(self.store.agent('dependent')['state'], 'running')
        self.assertEqual(len(self.monitor.calls), 2)

    def test_restart_recovers_exact_completed_task_without_observer_or_replay(self):
        receipt = delegate(self.store, self.args)
        self.store.update_agent('worker', state='interrupted', result='Coordinator stopped')
        self.store.close()
        self.store = Store(self.root / 'run')
        self.monitor.instance = None
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': receipt['swarm_task_id'],
                'status': 'completed', 'result': 'tests passed at evidence-42', 'request_id': 'complete'})
        finally:
            broker.close()
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'review')
        self.assertIn('evidence-42', self.store.agent('worker')['result'])
        self.assertEqual(len(self.monitor.calls), 1)

    def test_observer_loss_recovers_later_completion_but_not_rejected_review(self):
        receipt = delegate(self.store, self.args)
        self.monitor.instance = None
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'blocked')
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': receipt['swarm_task_id'],
                'status': 'completed', 'result': 'evidence', 'request_id': 'complete'})
        finally:
            broker.close()
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'review')
        self.store.update_agent('worker', state='blocked', result=json.dumps({'accepted': False}))
        tick(self.store)
        self.assertEqual(self.store.agent('worker')['state'], 'blocked')
        self.assertEqual(len(self.monitor.calls), 1)

    def test_corrective_pi_assignment_requires_terminal_report(self):
        first = delegate(self.store, self.args)
        self.store.update_agent('worker', state='blocked', result='Missing observer')
        repair = self.args | {'id': 'repair', 'supersedes': 'worker'}
        with self.assertRaisesRegex(ValueError, 'no terminal report'):
            delegate(self.store, repair)
        self.assertEqual(len(self.monitor.calls), 1)
        broker = self.broker()
        try:
            broker.call('peer', 'swarm_task', {'action': 'update', 'task_id': first['swarm_task_id'],
                'status': 'completed', 'result': 'Candidate needs repair', 'request_id': 'submit-old'})
        finally:
            broker.close()
        self.store.update_agent('worker', state='blocked', result=json.dumps({'accepted': False}))
        delegate(self.store, self.args | {'id': 'dependent', 'dependencies': ['worker']})
        second = delegate(self.store, repair)
        self.assertEqual(second['state'], 'running')
        self.assertEqual(second['dependent_recovery']['agents'], ['dependent'])
        self.assertEqual(self.store.agent('dependent')['state'], 'blocked')
        self.assertEqual(self.store.config()['assignment:worker']['superseded_by'], 'repair')
        self.assertEqual(delegate(self.store, repair), second)
        self.assertEqual(len(self.monitor.calls), 2)

    def test_coordinator_exit_preserves_independent_pi_but_interrupts_local_worker(self):
        from conductor.runtime import run_coordinator
        # Exercise cleanup after a provider failure, with room for the full swarm catalog.
        self.store.configure(context_budget=40000)
        delegate(self.store, self.args)
        self.store.add_agent('local', 'coordinator', 'Local process')
        self.store.update_agent('local', state='running')
        class Provider:
            usage = {}
            calls = 0
            def complete(self, *args):
                raise RuntimeError('Fixture API ceiling')
        with patch('conductor.runtime.Scheduler.tick'), self.assertRaisesRegex(RuntimeError, 'Fixture API ceiling'):
            run_coordinator(self.store, Provider())
        self.assertEqual(self.store.agent('worker')['state'], 'running')
        self.assertEqual(self.store.agent('local')['state'], 'interrupted')



    def test_corrective_assignment_can_replace_never_sent_blocked_dependency(self):
        self.store.add_agent('prerequisite', 'coordinator', 'Failed prerequisite')
        self.store.update_agent('prerequisite', state='failed')
        original = delegate(self.store, self.args | {'dependencies': ['prerequisite']})
        self.assertEqual(original['state'], 'blocked')
        self.assertFalse(self.monitor.calls)
        replacement = delegate(self.store, self.args | {'id': 'repair', 'supersedes': 'worker'})
        self.assertEqual(replacement['state'], 'running')
        self.assertEqual(len(self.monitor.calls), 1)


if __name__ == '__main__':
    unittest.main()
