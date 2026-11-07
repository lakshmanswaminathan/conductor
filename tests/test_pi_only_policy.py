import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.cli import initialize
from conductor.runtime import AgentLoop, Scheduler, coordinator_instructions
from conductor.swarm_runtime import perform


class PiOnlyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        repo = root / 'repo'
        repo.mkdir()
        self.store = initialize(root / 'run', repo, 'Manage Pi', demo=True, transport='process')
        self.store.configure(execution_policy='pi_only', allow_exec=True, computer_access=True,
                             swarm_root=str(root / 'swarm'))
        self.scheduler = Scheduler(self.store, root / 'model.sock')
        self.loop = AgentLoop(self.store, None, 'coordinator', self.scheduler)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_catalog_and_hidden_tool_boundary(self):
        prohibited = {'exec', 'dispatch', 'read_file', 'list_files', 'computer', 'cmux'}
        self.assertFalse(prohibited & self.loop.allowed)
        self.assertTrue({'dispatch_pi', 'review_task', 'pi_agents', 'codex_send', 'codex_status',
                         'history', 'search_context', 'swarm_task'} <= self.loop.allowed)
        for name in prohibited:
            self.loop.allowed.add(name)  # Catalog tampering cannot bypass execution policy.
            with self.assertRaisesRegex(ValueError, 'Pi-only'):
                self.loop.execute(name, {})
        with self.assertRaisesRegex(ValueError, 'Pi-only'):
            self.loop.command({'command': 'must not execute'})

    def test_scheduler_advances_pi_but_never_launches_local_workers(self):
        self.store.add_agent('legacy', 'coordinator', 'Old pending local assignment', [])
        with patch('conductor.pi_delegation.tick') as pi_tick, patch.object(self.scheduler, 'prepare') as prepare:
            self.scheduler.tick()
        pi_tick.assert_called_once_with(self.store)
        prepare.assert_not_called()
        self.assertEqual(self.store.agent('legacy')['state'], 'pending')

    def test_codex_routes_to_inbox_with_stable_chat_actor(self):
        with patch('conductor.codex_inbox.call', return_value={'status': 'queued'}) as inbox, \
             patch('conductor.swarm_runtime.SwarmBroker') as broker:
            perform(self.store, {'message': 'Report', 'request_id': 'report-1'}, 'codex_send')
            first = inbox.call_args.args[1]
            self.assertIn('conductor_send(chat_id=run,', inbox.call_args.args[3]['message'])
            self.assertIn('not new human authority', inbox.call_args.args[3]['message'])
            with self.assertRaisesRegex(ValueError, '3500'):
                perform(self.store, {'message': 'x' * 3501, 'request_id': 'too-long'}, 'codex_send')
            perform(self.store, {'request_id': 'report-1'}, 'codex_status')
            self.assertEqual(first, inbox.call_args.args[1])
            self.assertTrue(first.startswith('conductor:'))
            broker.assert_not_called()

    def test_native_engine_fails_closed_before_launch(self):
        from conductor.codex_runtime import run_codex_coordinator
        with patch('conductor.codex_runtime.AppServer') as server, patch('conductor.codex_runtime.codex_binary') as binary:
            with self.assertRaisesRegex(ValueError, 'Select engine=conductor'):
                run_codex_coordinator(self.store, 'fixture-key')
            server.assert_not_called()
            binary.assert_not_called()

    def test_default_cli_policy_unchanged(self):
        self.store.configure(execution_policy=None)
        legacy = AgentLoop(self.store, None, 'coordinator', self.scheduler)
        self.assertTrue({'exec', 'dispatch', 'read_file', 'list_files', 'computer', 'cmux'} <= legacy.allowed)
        self.assertIn('upstream orchestrator', coordinator_instructions({'execution_policy': 'pi_only'}))
        self.assertNotIn('Use dispatch for new local workers', coordinator_instructions({'execution_policy': 'pi_only'}))
