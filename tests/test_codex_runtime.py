import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import queue
import time
from types import SimpleNamespace
from conductor.runtime import Scheduler

from conductor.cli import initialize
from conductor.codex_runtime import run_codex_coordinator, codex_binary, AppServer, _coordination_input


class FakeServer:
    instances = []
    scenario = 'normal'
    hook = None
    turn_hook = None

    def __init__(self, binary, home, workspace, key, model):
        self.events = queue.Queue()
        self.serial = 0
        self.sent = []
        self.requests = []
        self.closed = False
        self.turns = 0
        self.instances.append(self)
        self.home = home
        self.model = model

    def emit(self, method, params):
        self.events.put({'method': method, 'params': params})

    def request(self, method, params):
        self.serial += 1
        ident = self.serial
        self.requests.append((method, params))
        result = {}
        if method in ('thread/start', 'thread/resume'):
            result = {'thread': {'id': 'native-thread'}}
        if method == 'turn/start':
            self.turns += 1
            result = {'turn': {'id': 'turn-' + str(self.turns)}}
        self.events.put({'id': ident, 'result': result})
        if method == 'turn/interrupt' and self.scenario == 'stale':
            self.emit('turn/completed', {'turn': {'id': params['turnId'], 'status': 'interrupted'}})
        if method == 'turn/start':
            if type(self).turn_hook:
                type(self).turn_hook(self)
            if self.scenario == 'exit':
                self.events.put({'_exit': True})
            elif self.scenario in ('cancel', 'hanging_interrupt'):
                type(self).hook()
            elif self.scenario == 'review' and self.turns == 2:
                self.events.put({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                    'threadId': 'native-thread', 'turnId': 'turn-2', 'callId': 'review-call',
                    'tool': 'review_task', 'arguments': {'agent': 'worker', 'accepted': True,
                    'summary': 'Checked reported artifact', 'evidence': ['artifact:fixture-proof']}}})
            elif self.scenario in ('tool', 'duplicate', 'stale', 'disabled', 'pending') and self.turns == 1:
                if self.scenario == 'stale':
                    type(self).hook()
                name = 'computer' if self.scenario == 'disabled' else 'list_files'
                self.events.put({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                    'threadId': 'native-thread', 'turnId': 'turn-1', 'callId': 'call-1',
                    'tool': name, 'arguments': {'path': '.'}}})
            else:
                self.complete()
        return ident

    def complete(self):
        self.emit('item/completed', {'item': {'type': 'agentMessage', 'id': 'answer-' + str(self.turns), 'text': '' if self.scenario == 'empty' else 'Native final', 'phase': None if self.scenario == 'unphased' else ('commentary' if self.scenario == 'commentary' else 'final_answer')}})
        self.emit('turn/completed', {'turn': {'id': 'turn-' + str(self.turns), 'status': 'completed'}})

    def send(self, value):
        self.sent.append(value)
        if value.get('id') == 'tool-request':
            if self.scenario == 'duplicate':
                self.scenario = 'tool'
                self.events.put({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                    'threadId': 'native-thread', 'turnId': 'turn-1', 'callId': 'call-1',
                    'tool': 'list_files', 'arguments': {'path': '.'}}})
            else:
                self.complete()

    def close(self):
        self.closed = True


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='conductor-native-test-')
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.store = initialize(self.root / 'run', self.repo, 'Original user request', demo=True, transport='process')
        self.store.configure(computer_access=False, max_turns=4, model='moonshotai/kimi-k3.1-pro')
        FakeServer.instances.clear()
        FakeServer.scenario = 'normal'
        FakeServer.hook = None
        FakeServer.turn_hook = None
        self.patch = patch('conductor.codex_runtime.AppServer', FakeServer)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.store.close()
        self.temp.cleanup()

    def run_native(self):
        run_codex_coordinator(self.store, 'fixture-only-key', binary='fake-codex')
        return FakeServer.instances[-1]

    def test_native_final_persistent_thread_resume_and_usage(self):
        first = self.run_native()
        self.assertEqual(self.store.agent('coordinator')['state'], 'done')
        self.assertEqual(self.store.config()['codex_thread_id'], 'native-thread')
        self.assertEqual(self.store.config()['codex_engine_usage']['native_api_calls'], None)
        self.assertFalse(self.store.config()['codex_engine_usage']['max_calls_enforced'])
        start = next(p for m, p in first.requests if m == 'thread/start')
        self.assertNotIn('finish', [t['name'] for t in start['dynamicTools']])
        self.assertEqual(start['sandbox'], 'read-only')
        self.assertEqual(start['model'], 'moonshotai/kimi-k3.1-pro')
        self.assertTrue(first.closed)
        self.store.event('user', '*', 'instruction', 'Follow-up request')
        second = self.run_native()
        self.assertIn('thread/resume', [m for m, _ in second.requests])
        turn = next(p for m, p in second.requests if m == 'turn/start')
        self.assertIn('Follow-up request', turn['input'][0]['text'])
        self.assertNotIn('Prior public conversation', turn['input'][0]['text'])

    def test_tool_exchange_executes_once_and_is_projected(self):
        FakeServer.scenario = 'duplicate'
        with patch('conductor.runtime.AgentLoop.execute', return_value={'ok': True}) as execute:
            self.run_native()
        execute.assert_called_once_with('list_files', {'path': '.'})
        messages = [m for _, m in self.store.history('coordinator')]
        self.assertEqual(len([m for m in messages if m.get('tool_calls')]), 1)
        self.assertEqual(len([m for m in messages if m['role'] == 'tool']), 1)

    def test_interrupted_call_is_never_replayed(self):
        self.store.append('coordinator', {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'function': {'name': 'list_files', 'arguments': '{}'}}]})
        FakeServer.scenario = 'pending'
        with patch('conductor.runtime.AgentLoop.execute') as execute:
            self.run_native()
        execute.assert_not_called()
        response = next(x for x in FakeServer.instances[-1].sent if x.get('id') == 'tool-request')
        self.assertFalse(response['result']['success'])
        self.assertIn('not replayed', response['result']['contentItems'][0]['text'])

    def test_new_user_context_fences_old_tools_and_steers(self):
        FakeServer.scenario = 'stale'
        FakeServer.hook = lambda: self.store.event('user', '*', 'instruction', 'Changed instruction')
        with patch('conductor.runtime.AgentLoop.execute') as execute:
            server = self.run_native()
        execute.assert_not_called()
        self.assertIn('turn/interrupt', [m for m, _ in server.requests])
        self.assertEqual(server.turns, 2)
        self.assertEqual(self.store.agent('coordinator')['state'], 'done')

    def test_disabled_computer_action_rejected(self):
        FakeServer.scenario = 'disabled'
        server = self.run_native()
        response = next(x for x in server.sent if x.get('id') == 'tool-request')
        self.assertFalse(response['result']['success'])
        self.assertIn('not enabled', response['result']['contentItems'][0]['text'])

    def test_cancel_and_process_failure(self):
        FakeServer.scenario = 'cancel'
        FakeServer.hook = lambda: self.store.configure(cancel_requested=True)
        server = self.run_native()
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')
        self.assertTrue(server.closed)
        self.store.configure(cancel_requested=False)
        FakeServer.scenario = 'exit'
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            self.run_native()
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')
        self.assertTrue(FakeServer.instances[-1].closed)

    def test_tool_ceiling(self):
        FakeServer.scenario = 'tool'
        self.store.configure(codex_max_tool_calls=1)
        with self.assertRaisesRegex(RuntimeError, 'tool ceiling'):
            self.run_native()
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')

    def test_commentary_only_is_not_completion(self):
        FakeServer.scenario = 'commentary'
        with self.assertRaisesRegex(RuntimeError, 'nonempty final'):
            self.run_native()
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')

    def test_unphased_final_and_empty_output(self):
        FakeServer.scenario = 'unphased'
        self.run_native()
        self.assertEqual(self.store.agent('coordinator')['state'], 'done')
        FakeServer.scenario = 'empty'
        with self.assertRaisesRegex(RuntimeError, 'nonempty final'):
            self.run_native()

    def test_deadline_is_bounded(self):
        self.store.configure(codex_timeout_seconds=0)
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.run_native()
        self.assertTrue(FakeServer.instances[-1].closed)
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')

    def test_nonzero_tool_outcome_is_failure_before_archival(self):
        FakeServer.scenario = 'duplicate'
        with patch('conductor.runtime.AgentLoop.execute', return_value={'exit_code': 2, 'output': 'fixture failure'}):
            server = self.run_native()
        replies = [x for x in server.sent if x.get('id') == 'tool-request']
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(not x['result']['success'] for x in replies))
        tools = [m for _, m in self.store.history('coordinator') if m['role'] == 'tool']
        self.assertFalse(tools[0]['_success'])

    def test_subprocess_isolation_and_cleanup(self):
        binary = self.root / 'fake-codex'
        binary.write_text('#!/usr/bin/env python3\nimport sys,json,os\nfor line in sys.stdin:\n m=json.loads(line)\n if "id" in m: print(json.dumps({"id":m["id"],"result":{"isolated":os.environ["CODEX_HOME"].endswith("codex-home"),"key_in_args":any("fixture-only-key" in x for x in sys.argv),"shell_disabled":"features.shell_tool=false" in sys.argv}}),flush=True)\n')
        binary.chmod(0o700)
        server = AppServer(binary, self.root / 'codex-home', self.root / 'workspace', 'fixture-only-key', 'test-model')
        try:
            ident = server.request('initialize', {})
            # This checks isolation and cleanup, not interpreter startup latency
            # on a busy desktop. A missing response still fails within ten seconds.
            result = server.events.get(timeout=10)
            self.assertEqual(result['id'], ident)
            self.assertEqual(result['result'], {'isolated': True, 'key_in_args': False, 'shell_disabled': True})
        finally:
            server.close()
        self.assertIsNotNone(server.process.poll())

    def test_interrupt_without_completion_is_bounded(self):
        FakeServer.scenario = 'hanging_interrupt'
        FakeServer.hook = lambda: self.store.event('user', '*', 'instruction', 'Stop obsolete work')
        self.store.configure(codex_interrupt_timeout_seconds=0)
        with self.assertRaisesRegex(RuntimeError, 'interrupt deadline'):
            self.run_native()
        self.assertTrue(FakeServer.instances[-1].closed)
        self.assertIn('turn/interrupt', [m for m, _ in FakeServer.instances[-1].requests])

    def test_user_update_during_index_keeps_exact_prompt_version(self):
        self.store.configure(computer_access=True)
        FakeServer.scenario = 'tool'
        indexed = []
        def catalog(**kwargs):
            if not indexed:
                indexed.append(True)
                self.store.event('user', '*', 'instruction', 'Arrived during index collection')
            return {}
        with patch('conductor.pi_monitor.monitor.catalog', side_effect=catalog), patch('conductor.runtime.AgentLoop.execute') as execute:
            server = self.run_native()
        execute.assert_not_called()
        turns = [p for m, p in server.requests if m == 'turn/start']
        self.assertEqual(len(turns), 2)
        self.assertNotIn('Arrived during index', turns[0]['input'][0]['text'])
        self.assertIn('Arrived during index', turns[1]['input'][0]['text'])

    def test_scheduler_advances_dependencies_without_model_polling(self):
        self.store.add_agent('a', 'coordinator', 'First stage')
        self.store.add_agent('b', 'coordinator', 'Second stage', ['a'])
        self.store.configure(max_turns=8, codex_scheduler_interval_seconds=0.05)
        test = self
        class AdvancingScheduler(Scheduler):
            def __init__(self, *args):
                super().__init__(*args)
                self.ticks = 0
            def tick(self):
                self.ticks += 1
                if self.ticks == 4:
                    test.assertEqual(FakeServer.instances[-1].turns, 1, 'No API spin while worker state is unchanged')
                    self.store.update_agent('a', state='done', result='First stage VERIFIED_SENTINEL')
                    self.store.merge('a', [{'key':'proof','value':'verified finding','evidence':['artifact:a']}])
                    self.store.event('a', 'coordinator', 'message', 'Review first stage evidence')
                if self.ticks == 7:
                    test.assertEqual(self.store.agent('b')['state'], 'starting')
                    self.store.update_agent('b', state='done', result='Second stage complete')
                super().tick()
        launched = []
        def launch(command, **kwargs):
            launched.append(command[command.index('--agent') + 1])
            return SimpleNamespace(poll=lambda: 0, wait=lambda **kw: 0)
        with patch('conductor.codex_runtime.Scheduler', AdvancingScheduler), patch('conductor.runtime.subprocess.Popen', side_effect=launch):
            server = self.run_native()
        self.assertEqual(launched, ['a', 'b'])
        self.assertEqual(server.turns, 3)
        turns = [p['input'][0]['text'] for m, p in server.requests if m == 'turn/start']
        self.assertIn('VERIFIED_SENTINEL', turns[1])
        self.assertIn('verified finding', turns[1])
        self.assertIn('Review first stage evidence', turns[1])
        self.assertIn('Synthesize these results', turns[1])
        self.assertIn('Second stage complete', turns[2])
        self.assertEqual(self.store.agent('coordinator')['state'], 'done')
        self.assertIsNone(self.store.config()['codex_pending_turn'])
        self.assertIn('worker:b', self.store.config()['codex_coordination_checkpoint'])

    def test_worker_finishing_during_model_turn_requires_synthesis(self):
        self.store.add_agent('worker', 'coordinator', 'Work')
        self.store.update_agent('worker', state='running', heartbeat=time.time())
        def finish_worker(server):
            if server.turns == 1:
                self.store.update_agent('worker', state='failed', result='FAILURE_EVIDENCE: acceptance test failed')
        FakeServer.turn_hook = finish_worker
        with patch('conductor.codex_runtime.Scheduler.tick'):
            server = self.run_native()
        self.assertEqual(server.turns, 2)
        turns = [p['input'][0]['text'] for m, p in server.requests if m == 'turn/start']
        self.assertNotIn('FAILURE_EVIDENCE', turns[0])
        self.assertIn('FAILURE_EVIDENCE', turns[1])
        self.assertEqual(self.store.agent('coordinator')['state'], 'blocked')
        # Persisted checkpoint prevents unchanged result reinjection after native resume.
        with patch('conductor.codex_runtime.Scheduler.tick'):
            resumed = self.run_native()
        prompt = next(p['input'][0]['text'] for m, p in resumed.requests if m == 'turn/start')
        self.assertNotIn('FAILURE_EVIDENCE', prompt)

    def test_waiting_workers_wake_for_user_and_can_cancel_without_api_spin(self):
        self.store.add_agent('worker', 'coordinator', 'Waiting task')
        self.store.update_agent('worker', state='running', heartbeat=time.time())
        self.store.configure(codex_scheduler_interval_seconds=0.05)
        ticks = []
        def tick():
            ticks.append(1)
            if len(ticks) == 4:
                self.assertEqual(FakeServer.instances[-1].turns, 1)
                self.store.event('user', '*', 'instruction', 'New priority while waiting')
                self.store.update_agent('worker', state='failed', result='User stopped previous task')
        with patch('conductor.codex_runtime.Scheduler.tick', side_effect=tick):
            server = self.run_native()
        turns = [p['input'][0]['text'] for m, p in server.requests if m == 'turn/start']
        self.assertEqual(len(turns), 2)
        self.assertIn('New priority while waiting', turns[1])
        self.store.update_agent('worker', state='running', heartbeat=time.time(), result=None)
        ticks.clear()
        def cancel_tick():
            ticks.append(1)
            if len(ticks) == 4:
                self.assertEqual(FakeServer.instances[-1].turns, 1)
                self.store.configure(cancel_requested=True)
        with patch('conductor.codex_runtime.Scheduler.tick', side_effect=cancel_tick):
            cancelled = self.run_native()
        self.assertEqual(cancelled.turns, 1)
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')

    def test_supervision_projection_is_bounded_and_checkpoints_only_included_updates(self):
        records = {'finding:' + str(i): {'id': i, 'source': 'worker', 'evidence': ['large evidence ' * 500] * 20} for i in range(8)}
        text, checkpoint = _coordination_input(records, {})
        self.assertLess(len(text.encode()), 14000)
        self.assertGreater(len(checkpoint), 0)
        self.assertLess(len(checkpoint), len(records))
        self.assertIn('truncated', text)
        self.assertIn('omitted_updates', text)
        seen = len(checkpoint)
        _, checkpoint = _coordination_input(records, checkpoint)
        self.assertGreater(len(checkpoint), seen)

    def test_unresolved_historic_failure_cannot_disappear_on_followup(self):
        self.store.configure(ui=True)
        self.store.add_agent('old', 'coordinator', 'Historical task')
        self.store.update_agent('old', state='failed', result='HISTORIC_FAILURE')
        self.store.configure(**{'assignment:old': {'scope': 0}})
        event = self.store.event('user', '*', 'instruction', 'Independent current request')
        with patch('conductor.codex_runtime.Scheduler.tick'):
            server = self.run_native()
        self.assertEqual(self.store.config()['orchestration_scope'], event)
        self.assertEqual(self.store.agent('coordinator')['state'], 'blocked')
        prompt = next(p['input'][0]['text'] for m, p in server.requests if m == 'turn/start')
        self.assertIn('HISTORIC_FAILURE', prompt)

    def test_review_is_required_and_accepted_through_tool_before_final(self):
        self.store.configure(ui=True, max_turns=5)
        self.store.add_agent('worker', 'coordinator', 'Submitted task')
        self.store.update_agent('worker', state='review', result='Reported verified fixture')
        self.store.configure(**{'assignment:worker': {'scope': self.store.user_context()[-1]['id'], 'review_required': True,
            'ownership': ['fixture'], 'acceptance': ['fixture proof exists']}})
        FakeServer.scenario = 'review'
        with patch('conductor.codex_runtime.Scheduler.tick'):
            server = self.run_native()
        self.assertEqual(self.store.agent('worker')['state'], 'done')
        self.assertEqual(self.store.agent('coordinator')['state'], 'done')
        self.assertEqual(server.turns, 3)
        prompts = [p['input'][0]['text'] for m, p in server.requests if m == 'turn/start']
        self.assertIn('Coordinator acceptance required', prompts[1])
        self.assertIn('Checked reported artifact', prompts[2])

    def test_ignored_review_is_bounded_not_final_or_silent_wait(self):
        self.store.add_agent('worker', 'coordinator', 'Needs review')
        self.store.update_agent('worker', state='review', result='Not accepted')
        self.store.configure(max_turns=2)
        with patch('conductor.codex_runtime.Scheduler.tick'), self.assertRaisesRegex(RuntimeError, 'turn ceiling'):
            self.run_native()
        self.assertEqual(self.store.agent('worker')['state'], 'review')
        self.assertEqual(self.store.agent('coordinator')['state'], 'interrupted')
        self.assertEqual(FakeServer.instances[-1].turns, 2)

    def test_catalog_upgrade_preserves_authoritative_instructions_and_history(self):
        self.store.event('user', '*', 'instruction', 'CONSTRAINT_ALPHA never publish')
        self.run_native()
        original = self.store.history('coordinator')
        self.store.event('user', '*', 'instruction', 'CONSTRAINT_BETA inspect evidence')
        self.store.configure(codex_thread_id='obsolete-thread', codex_tool_catalog_hash='obsolete')
        server = self.run_native()
        self.assertIn('thread/start', [m for m, _ in server.requests])
        self.assertNotIn('thread/resume', [m for m, _ in server.requests])
        self.assertIn('obsolete-thread', self.store.config()['codex_previous_thread_ids'])
        prompt = next(p['input'][0]['text'] for m, p in server.requests if m == 'turn/start')
        self.assertIn('CONSTRAINT_ALPHA', prompt)
        self.assertIn('CONSTRAINT_BETA', prompt)
        self.assertIn('Prior public conversation', prompt)
        self.assertEqual(self.store.history('coordinator')[:len(original)], original)
        upgrade = self.store.db.execute("SELECT body FROM events WHERE kind='harness_upgrade'").fetchone()
        self.assertIn('obsolete-thread', upgrade['body'])
        resumed = self.run_native()
        self.assertIn('thread/resume', [m for m, _ in resumed.requests])

    def test_model_switch_updates_provider_and_native_resume_without_catalog_reset(self):
        self.run_native()
        before = self.store.config()
        self.store.configure(model='synthetic-provider/exact-new-model-id')
        self.store.event('user', '*', 'instruction', 'Continue with the selected model')
        resumed = self.run_native()
        self.assertEqual(resumed.model, 'synthetic-provider/exact-new-model-id')
        params = next(p for method, p in resumed.requests if method == 'thread/resume')
        self.assertEqual(params['model'], 'synthetic-provider/exact-new-model-id')
        self.assertEqual(params['threadId'], before['codex_thread_id'])
        self.assertNotIn('thread/start', [method for method, _ in resumed.requests])
        self.assertEqual(self.store.config()['codex_tool_catalog_hash'], before['codex_tool_catalog_hash'])
        self.assertEqual(self.store.config().get('codex_previous_thread_ids', []), before.get('codex_previous_thread_ids', []))

    def test_missing_binary_is_explicit(self):
        with patch('conductor.codex_runtime.shutil.which', return_value=None), patch('conductor.codex_runtime.Path.is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Install the Codex CLI'):
                codex_binary()
