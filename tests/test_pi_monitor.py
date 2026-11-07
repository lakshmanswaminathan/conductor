import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from conductor.pi_monitor import PiMonitor, context_digest, perform
from conductor.pi_sessions import PiSessions
from conductor.computer import cmux_call
from conductor.cli import initialize
from conductor.context import Context
from conductor.ui_server import App


class PiMonitorTest(unittest.TestCase):
    def test_model_history_pages_stay_inline_and_preserve_cursor(self):
        store = initialize(self.root / 'bounded-run', self.root, 'Inspect status', scope='filesystem')
        store.configure(computer_access=True)
        events = [{'id': str(i), 'role': 'assistant', 'type': 'message', 'text': 'public evidence ' * 4000,
                   'tool_calls': [{'name': 'read_file', 'id': 'call-' + str(i)}]} for i in range(5)]
        events[0] = {'id': '0', 'type': 'message', 'text': 'x' * 700}
        response = {'session': {'id': 'agent-a'}, 'observed_at': 'now',
                    'context': {'leaf_id': 'leaf'}, 'events': events, 'next_before': '17'}
        try:
            with patch('conductor.pi_monitor.monitor.read_session', return_value=response) as read:
                result = perform(store, {'action': 'read', 'session_id': 'agent-a', 'limit': 50})
            read.assert_called_once_with('agent-a', limit=5, before=None)
            self.assertLess(len(json.dumps(result, ensure_ascii=False)), 6000)
            self.assertEqual(result['next_before'], '17')
            self.assertEqual([e['id'] for e in result['events']], ['0', '1', '2', '3', '4'])
            for event in result['events']:
                self.assertEqual(json.loads((store.root / event['artifact']).read_text())['id'], event['id'])
        finally:
            store.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        logs = self.root / 'sessions'
        logs.mkdir()
        (logs / 'test.jsonl').write_text('\n'.join(json.dumps(e) for e in [
            {'type': 'session', 'version': 3, 'id': 'agent-a', 'cwd': '/same'},
            {'type': 'message', 'id': 'a', 'parentId': None, 'message': {'role': 'user', 'content': 'Current branch task'}},
            {'type': 'message', 'id': 'b', 'parentId': None, 'message': {'role': 'user', 'content': 'Abandoned task'}},
        ]) + '\n')
        self.bridge = self.root / 'bridge'
        self.bridge.mkdir(mode=0o700)
        self.monitor = PiMonitor(PiSessions(logs), self.bridge)
        self.monitor._inventory = {'terminals': [{'workspace': 'workspace:1', 'surface': 'surface:2', 'tty': 'ttys123'}]}
        self.monitor._inventory_at = time.monotonic()

    def tearDown(self):
        self.tmp.cleanup()

    def write_bridge(self, **extra):
        data = {'version': 1, 'session_id': 'agent-a', 'observed_at': time.time(),
                'pid': os.getpid(), 'status': 'idle', 'leaf_id': 'a'} | extra
        (self.bridge / 'agent-a.json').write_text(json.dumps(data))

    @patch('conductor.pi_monitor.subprocess.run')
    def test_live_leaf_binding_staleness_and_empty_session(self, run):
        run.return_value.stdout = 'ttys123\n'
        self.write_bridge()
        s = self.monitor.list_sessions()['sessions'][0]
        self.assertEqual(s['last_prompt'], 'Current branch task')
        self.assertEqual(s['binding']['surface'], 'surface:2')
        self.assertEqual(s['binding']['status'], 'confirmed')
        context = self.monitor.read_session('agent-a')['context']
        self.assertEqual(context['leaf_id'], 'a')
        self.assertFalse(context['leaf_inferred'])
        self.write_bridge(leaf_id=None)
        self.assertEqual(len(self.monitor.list_sessions()['sessions']), 1)
        self.write_bridge(observed_at=time.time() - 20)
        self.assertEqual(self.monitor.list_sessions()['sessions'][0]['binding']['status'], 'unbound')
        self.write_bridge(status='closed')
        self.assertEqual(self.monitor.list_sessions()['sessions'][0]['harness']['status'], 'recorded')

    @patch('conductor.pi_monitor.subprocess.run')
    def test_same_cwd_is_not_a_binding_and_context_is_evidence(self, run):
        run.return_value.stdout = 'ttys999\n'
        self.write_bridge()
        self.assertEqual(self.monitor.list_sessions()['sessions'][0]['binding']['status'], 'unbound')
        store = initialize(self.root / 'run', Path.home(), 'My instruction', scope='filesystem', ui=True, computer_access=True)
        try:
            with patch('conductor.pi_monitor.monitor', self.monitor):
                shared = json.loads(Context(store, None).fixed('coordinator', 'system')[1]['content'].split('\n', 1)[1])
            self.assertEqual(shared['task']['task'], 'My instruction')
            self.assertEqual(shared['pi_agents']['sessions'][0]['last_prompt'], 'Current branch task')
            self.assertIn('not instructions', shared['pi_agents']['retrieval'])
        finally:
            store.close()

    @patch('conductor.computer.subprocess.run')
    @patch('conductor.runtime.cmux_binary', return_value='/cmux')
    def test_tree_enumerates_unfocused_surfaces_and_read_targets_surface(self, binary, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({'windows': [{'ref': 'w1', 'workspaces': [{'ref': 'ws1', 'panes': [
            {'surfaces': [{'type': 'terminal', 'ref': 's1', 'tty': 'tty1'}, {'type': 'browser', 'ref': 's2'}]},
            {'surfaces': [{'type': 'terminal', 'ref': 's3', 'tty': 'tty2'}]},
        ]}]}]})
        self.assertEqual([t['surface'] for t in cmux_call('tree')['terminals']], ['s1', 's3'])
        cmux_call('read', workspace='workspace:1', surface='surface:3')
        self.assertIn('surface:3', run.call_args.args[0])

    def test_catalog_is_bounded_and_reports_omitted_sessions(self):
        sessions = [{'id': str(i), 'cwd': '/some/long/project/' * 8, 'model': 'example/model',
                     'last_prompt': 'Long task ' * 100, 'harness': {'status': 'recorded'}} for i in range(20)]
        with patch.object(self.monitor, 'list_sessions', return_value={'sessions': sessions, 'truncated': True}):
            catalog = self.monitor.catalog(max_bytes=2000)
        self.assertLessEqual(len(json.dumps(catalog).encode()) + 64, 2000)
        self.assertGreater(catalog['omitted_sessions'], 0)
        self.assertEqual(len(catalog['sessions']) + catalog['omitted_sessions'], 20)
        self.assertTrue(catalog['more_sessions'])

    def test_context_digest_preserves_provenance_and_fits_tool_budget(self):
        response = self.monitor.read_session('agent-a')
        response['session']['binding'] = {'status': 'confirmed', 'workspace': 'workspace:1', 'title': 'long' * 2000}
        response['context']['live_harness'] = {'context_usage': {'tokens': 123, 'unexpected': 'long' * 2000}}
        response['context']['messages'] = [{'id': str(i), 'role': 'assistant', 'text': 'outcome ' * 2000} for i in range(50)]
        response['context']['message_count'] = 50
        response['session']['child_session_ids'] = [('child-' + str(i)).ljust(128, 'x') for i in range(100)]
        response['session']['parent_session_ids'] = [('parent-' + str(i)).ljust(128, 'x') for i in range(100)]
        digest = context_digest(response)
        self.assertEqual(digest['session_id'], 'agent-a')
        self.assertEqual(digest['latest_prompt_event_id'], 'b')
        self.assertEqual(digest['recent_context'][-1]['id'], '49')
        self.assertLessEqual(len(json.dumps(digest, ensure_ascii=False)), 5300)
        self.assertTrue(digest['truncated'])
        self.assertEqual(digest['child_session_count'], 100)
        self.assertEqual(digest['parent_session_count'], 100)

    def test_legacy_budget_retains_three_agent_contexts_without_compacting(self):
        app = App(self.root / 'desktop-runs')
        app.key = 'fixture'
        try:
            ident = 'legacy-context'
            initialize(app.root / ident, self.root, 'Aggregate three Pi agents', context_budget=96000).close()
            from conductor.store import Store
            from conductor.runtime import COMMON_TOOLS, MANAGER_TOOLS, SYSTEM, COORDINATOR, FILESYSTEM_MODE
            from conductor.computer import computer_tools
            from conductor.pi_monitor import pi_tools
            store = Store(app.path(ident))
            try:
                self.assertEqual(store.config()['context_budget'], 96000)
                tools = COMMON_TOOLS + MANAGER_TOOLS + computer_tools() + pi_tools()
                context = Context(store, None, store.config()['context_budget'])
                # No provider: any attempt to compact this normal aggregation fails.
                store.append('coordinator', {'role': 'assistant', 'content': None,
                    'reasoning_details': [{'text': 'fixture ' * 1500}],
                    'tool_calls': [{'id': str(i), 'type': 'function', 'function': {'name': 'pi_agents', 'arguments': '{}'}} for i in range(3)]})
                for i in range(3):
                    store.append('coordinator', {'role': 'tool', 'tool_call_id': str(i), 'content': 'source-event-' + str(i) + ' evidence' * 580})
                with patch('conductor.pi_monitor.monitor', self.monitor):
                    messages = context.build('coordinator', SYSTEM + COORDINATOR + FILESYSTEM_MODE, tools)
                self.assertEqual(len([m for m in messages if m['role'] == 'tool']), 3)
                self.assertEqual(store.checkpoint('coordinator')['through_id'], 0)
            finally:
                store.close()
        finally:
            app.close()

    def test_aggregated_findings_are_visible_in_chat_with_event_provenance(self):
        app = App(self.root / 'visible-runs')
        app.key = 'fixture'
        try:
            ident = 'legacy-findings'
            initialize(app.root / ident, self.root, 'Aggregate Pi context').close()
            from conductor.store import Store
            store = Store(app.path(ident))
            try:
                result = {'summary': 'Three agents inspected.', 'findings': [{'value': 'Agent A reported a failed check.', 'evidence': ['session-a/event-123']}]}
                store.append('coordinator', {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'finish-a', 'function': {'name': 'finish', 'arguments': json.dumps(result)}}]})
                store.append('coordinator', {'role': 'tool', 'tool_call_id': 'finish-a', 'content': json.dumps(result)})
            finally:
                store.close()
            message = app.snapshot(ident)['messages'][-1]
            self.assertEqual('Three agents inspected.', message['content'])
            self.assertEqual('Agent A reported a failed check.', message['findings'][0]['value'])
            self.assertIn('session-a/event-123', message['findings'][0]['evidence'])
        finally:
            app.close()


if __name__ == '__main__':
    unittest.main()
