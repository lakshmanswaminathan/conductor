import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from conductor.cli import initialize, main
from conductor.context import Context, ContextOverflow, rounds, size
from conductor.demo import DemoProvider
from conductor.provider import Broker, OpenRouter, RemoteProvider
from conductor.runtime import AgentLoop, Scheduler, SYSTEM, WORKER, git, run_coordinator


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='conductor-test-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'README.md').write_text('Test repository\n')
        self.store = initialize(self.root / 'run', self.repo, 'Keep user constraints exact.', demo=True, transport='process')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, ident, deps=()):
        self.store.add_agent(ident, 'coordinator', 'Bounded task', deps)

    def test_merge_idempotency_conflicts_and_user_precedence(self):
        self.add('a')
        self.add('b')
        a = [{'key': 'db', 'value': 'SQLite', 'evidence': ['a:12']}]
        self.store.merge('a', a)
        self.store.merge('a', a)
        self.assertEqual(len(self.store.claims()), 1)
        self.store.merge('b', [{'key': 'db', 'value': 'JSON', 'evidence': ['b:9']}])
        self.assertEqual([x['status'] for x in self.store.claims()], ['conflict', 'conflict'])
        self.store.resolve('db', 'SQLite')
        self.store.merge('b', [{'key': 'db', 'value': 'Postgres', 'evidence': ['b:22']}])
        accepted = [c for c in self.store.claims() if c['source'] == 'user']
        self.assertEqual(accepted[0]['status'], 'accepted')
        self.assertIn('Decision for db: SQLite', [m['body'] for m in self.store.user_context()])

    def test_invalid_merge_is_atomic(self):
        self.add('a')
        with self.assertRaises(ValueError):
            self.store.merge('a', [{'key': 'ok', 'value': 'yes', 'evidence': []}, {'key': ''}])
        self.assertEqual(self.store.claims(), [])

    def test_compaction_keeps_raw_history_and_whole_tool_exchanges(self):
        self.add('a')
        self.store.event('user', '*', 'instruction', 'Never change the public API. Exact sentinel: μ-42.')
        for i in range(30):
            self.store.append('a', {'role': 'assistant', 'content': None, 'reasoning_details': [{'text': 'PRIVATE_REASONING_SENTINEL'}],
                'tool_calls': [{'id': str(i), 'function': {'name': 'read_file', 'arguments': '{}'}, 'type': 'function'}]})
            self.store.append('a', {'role': 'tool', 'tool_call_id': str(i), 'content': 'evidence ' * 160})
        original = self.store.history('a')
        provider = DemoProvider()
        messages = Context(self.store, provider, 5000).build('a', 'Agent instructions')
        self.assertLessEqual(size(messages) + size([]), 5000)
        self.assertEqual(self.store.history('a'), original)
        self.assertGreater(self.store.checkpoint('a')['through_id'], 0)
        self.assertIn('μ-42', json.dumps(messages, ensure_ascii=False))
        self.assertIn(self.store.checkpoint('a')['through_id'], [i for i, m in original if m['role'] == 'tool'])
        rounds([(i, m) for i, m in enumerate(messages)])

    def test_oversized_pins_fail_without_dropping_context(self):
        self.add('a')
        self.store.event('user', '*', 'instruction', 'x' * 8000)
        with self.assertRaises(ContextOverflow):
            Context(self.store, DemoProvider(), 5000).build('a', 'system')
        self.assertEqual(self.store.checkpoint('a')['through_id'], 0)

    def test_compactor_never_sees_provider_reasoning(self):
        self.add('a')
        for i in range(10):
            self.store.append('a', {'role': 'assistant', 'content': 'Evidence ' * 200,
                                    'reasoning_details': [{'text': 'DO_NOT_BROADCAST'}]})
        class Summary:
            def complete(inner, messages, tools, max_tokens=700):
                self.assertNotIn('DO_NOT_BROADCAST', json.dumps(messages))
                self.assertGreaterEqual(max_tokens, 4096)
                return {'content': 'Evidence summary, source events retained.'}
        Context(self.store, Summary(), 5000).build('a', 'system')

    def test_empty_compaction_does_not_advance_checkpoint(self):
        self.add('a')
        for _ in range(2):
            self.store.append('a', {'role': 'assistant', 'content': 'x' * 3000})
        class BadSummary:
            def complete(inner, *args, **kwargs):
                return {'content': ''}
        with self.assertRaises(ContextOverflow):
            Context(self.store, BadSummary(), 5000).build('a', 'system')
        self.assertEqual(self.store.checkpoint('a')['through_id'], 0)

    def test_pending_tools_are_not_compacted(self):
        with self.assertRaises(ValueError):
            rounds([(1, {'role': 'assistant', 'tool_calls': [{'id': 'unfinished'}]})])

    def test_new_user_context_arrives_before_next_tool(self):
        self.add('a')
        store = self.store
        class Steering:
            calls = 0
            def complete(inner, messages, tools, **kwargs):
                inner.calls += 1
                if inner.calls == 1:
                    store.event('user', '*', 'instruction', 'New decision: retain both competing findings.')
                else:
                    self.assertIn('New decision', json.dumps(messages))
                    self.assertIn('User context changed', json.dumps(messages))
                return {'tool_calls': [{'id': str(inner.calls), 'type': 'function', 'function': {
                    'name': 'finish', 'arguments': json.dumps({'summary': 'Updated instructions honored.', 'outcome': 'done', 'findings': []})}}]}
        provider = Steering()
        AgentLoop(store, provider, 'a').run()
        self.assertEqual(provider.calls, 2)
        self.assertEqual(store.agent('a')['state'], 'done')

    def test_followup_after_finish_is_a_new_user_turn_and_current_task(self):
        self.store.configure(ui=True, computer_access=True)
        self.store.deliver_user_instructions('coordinator')
        self.store.append('coordinator', {'role': 'assistant', 'content': 'Test acknowledged.'})
        self.store.update_agent('coordinator', state='done')
        followup = 'Inspect the running cmux windows.'
        self.store.event('user', '*', 'instruction', followup)
        class Provider:
            def complete(inner, messages, tools, **kwargs):
                self.assertEqual(messages[-1], {'role': 'user', 'content': followup})
                snapshot = json.loads(messages[1]['content'].split('\n', 1)[1])
                self.assertEqual(snapshot['task']['task'], followup)
                self.assertIn('cmux', [t['function']['name'] for t in tools])
                return {'tool_calls': [{'id': 'followup-finish', 'type': 'function', 'function': {
                    'name': 'finish', 'arguments': json.dumps({'summary': 'Follow-up received.', 'outcome': 'done', 'findings': []})}}]}
        scheduler = type('SchedulerStub', (), {'tick': lambda self: None})()
        AgentLoop(self.store, Provider(), 'coordinator', scheduler).run()
        self.store.deliver_user_instructions('coordinator')
        turns = [m for _, m in self.store.history('coordinator')]
        self.assertEqual(sum(m.get('content') == followup for m in turns), 1)
        self.assertLess(next(i for i,m in enumerate(turns) if m.get('content') == 'Test acknowledged.'),
                        next(i for i,m in enumerate(turns) if m.get('content') == followup))

    def test_resume_closes_unknown_tool_exchange_without_replay(self):
        self.add('a')
        self.store.append('a', {'role': 'assistant', 'tool_calls': [{'id': 'old', 'type': 'function', 'function': {'name': 'exec', 'arguments': '{"command":"false"}'}}]})
        class Finish:
            def complete(inner, messages, tools, **kwargs):
                self.assertIn('execution outcome unknown', json.dumps(messages))
                return {'tool_calls': [{'id': 'finish', 'type': 'function', 'function': {'name': 'finish', 'arguments': json.dumps({'summary': 'Recovery inspected.', 'outcome': 'done', 'findings': []})}}]}
        AgentLoop(self.store, Finish(), 'a').run()
        rounds(self.store.history('a'))
        self.assertEqual(self.store.agent('a')['state'], 'done')

    def test_broker_workers_never_receive_api_key(self):
        socket = self.root / 'model.sock'
        class Provider:
            def complete(inner, messages, tools, max_tokens):
                return {'role': 'assistant', 'content': 'broker roundtrip'}
        with Broker(socket, Provider()):
            self.assertEqual(socket.stat().st_mode & 0o777, 0o600)
            result = RemoteProvider(socket).complete([{'role': 'user', 'content': 'hello'}], [])
        self.assertEqual(result['content'], 'broker roundtrip')
        self.assertFalse(socket.exists())

    def test_process_end_to_end_three_agents_and_handoff(self):
        run_coordinator(self.store, DemoProvider())
        self.assertEqual({a['state'] for a in self.store.agents()}, {'done'})
        self.assertEqual(len(self.store.agents()), 4)
        self.assertIn('2 conflicting claims', self.store.agent('review')['result'])
        self.assertEqual(len([c for c in self.store.claims() if c['status'] == 'conflict']), 2)

    def test_failed_dependencies_block_dispatch(self):
        self.add('a')
        self.add('b', ['a'])
        self.store.update_agent('a', state='failed')
        Scheduler(self.store, self.root / 'absent.sock').tick()
        self.assertEqual(self.store.agent('b')['state'], 'blocked')

    def test_worker_claim_is_exclusive(self):
        self.add('a')
        self.store.update_agent('a', state='starting')
        self.assertTrue(self.store.claim_worker('a'))
        self.assertFalse(self.store.claim_worker('a'))

    def test_read_tools_reject_symlink_escape(self):
        self.add('a')
        (self.repo / 'escape').symlink_to(self.root)
        loop = AgentLoop(self.store, DemoProvider(), 'a')
        with self.assertRaises(ValueError):
            loop.execute('read_file', {'path': 'escape/run/manifest.json'})
        with self.assertRaises(ValueError):
            loop.execute('exec', {'command': 'true'})

    def test_dependency_patches_apply_once_and_keep_parent_checkout_clean(self):
        git(self.repo, 'init')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Initial')
        self.store.configure(allow_exec=True, base=git(self.repo, 'rev-parse', 'HEAD'))
        self.add('a')
        scheduler = Scheduler(self.store, self.root / 'unused.sock')
        a = scheduler.prepare(self.store.agent('a'))
        self.store.update_agent('a', cwd=str(a), state='running')
        (a / 'added.txt').write_text('a   \n')
        AgentLoop(self.store, DemoProvider(), 'a').finish({'summary': 'Created file', 'outcome': 'done', 'findings': []})
        self.add('b', ['a'])
        b = scheduler.prepare(self.store.agent('b'))
        self.assertEqual((b / 'added.txt').read_text(), 'a   \n')
        self.store.update_agent('b', cwd=str(b), state='running')
        (b / 'added.txt').write_text('a\nb\n')
        AgentLoop(self.store, DemoProvider(), 'b').finish({'summary': 'Extended file', 'outcome': 'done', 'findings': []})
        self.add('c', ['a', 'b'])
        c = scheduler.prepare(self.store.agent('c'))
        self.assertEqual((c / 'added.txt').read_text(), 'a\nb\n')
        self.assertFalse((self.repo / 'added.txt').exists())
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')

    def test_shell_tool_timeout_is_reported_and_output_archived(self):
        self.store.configure(allow_exec=True)
        self.add('a')
        self.store.update_agent('a', state='running')
        result = AgentLoop(self.store, DemoProvider(), 'a').execute('exec', {'command': 'printf before; sleep 20', 'timeout': 1})
        self.assertNotEqual(result['exit_code'], 0)
        self.assertIsNotNone(result['stopped'])
        self.assertEqual((self.store.root / result['artifact']).read_text(), 'before')

    def test_openrouter_replays_reasoning_and_enforces_request_ceiling(self):
        provider = OpenRouter('deepseek/deepseek-v3.2', key='test-placeholder', max_calls=1)
        message = {'role': 'assistant', 'content': 'answer', 'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'opaque'}]}
        import io
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'choices': [{'message': message, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10}}).encode())) as request:
            self.assertEqual(provider.complete([message], []), message)
            self.assertEqual(json.loads(request.call_args.args[0].data)['messages'][0], message)
        with self.assertRaisesRegex(RuntimeError, 'ceiling'):
            provider.complete([], [])
        self.assertEqual(provider.usage['prompt_tokens'], 10)

    def test_api_ceiling_and_usage_survive_provider_restart(self):
        self.store.configure(api_calls=7, usage={'prompt_tokens': 42})
        provider = OpenRouter('test', key='test-placeholder', max_calls=7, state_path=self.store.root)
        with self.assertRaisesRegex(RuntimeError, 'ceiling'):
            provider.complete([], [])
        self.assertEqual(provider.calls, 7)
        self.assertEqual(provider.usage['prompt_tokens'], 42)

    def test_user_correction_during_context_build_rejects_stale_action(self):
        self.add('a')
        original = Context.build
        count = [0]
        def build(context, *args):
            result = original(context, *args)
            count[0] += 1
            if count[0] == 1:
                self.store.event('user', '*', 'instruction', 'Correction during context construction')
            return result
        class Finish:
            calls = 0
            def complete(inner, messages, tools, **kwargs):
                inner.calls += 1
                return {'tool_calls': [{'id': str(inner.calls), 'type': 'function', 'function': {'name': 'finish', 'arguments': json.dumps({'summary': 'Done', 'outcome': 'done', 'findings': []})}}]}
        provider = Finish()
        with patch.object(Context, 'build', build):
            AgentLoop(self.store, provider, 'a').run()
        self.assertEqual(provider.calls, 2)

    def test_large_shared_memory_is_retrievable_without_overflow(self):
        self.add('a')
        for i in range(20):
            self.store.merge('a', [{'key': f'finding-{i}', 'value': 'x' * 2000, 'evidence': ['fixture']}])
        snapshot = Context(self.store, DemoProvider(), 12000).build('a', 'system')
        self.assertLess(size(snapshot), 12000)
        state = json.loads(snapshot[1]['content'].split('\n', 1)[1])
        self.assertGreater(state['omitted_findings'], 0)
        result = AgentLoop(self.store, DemoProvider(), 'a').execute('search_context', {'kind': 'findings', 'query': 'finding-19'})
        self.assertEqual(result[0]['key'], 'finding-19')

    def test_sigterm_worker_stops_its_shell_process_group(self):
        self.store.configure(allow_exec=True)
        self.add('a')
        self.store.update_agent('a', state='starting', cwd=str(self.repo))
        pid_file = self.repo / 'child.pid'
        import shlex
        code = 'import os,time; from pathlib import Path; Path("child.pid").write_text(str(os.getpid())); time.sleep(60)'
        command = shlex.join([sys.executable, '-c', code])
        class Commands:
            def complete(inner, *args):
                return {'role': 'assistant', 'tool_calls': [{'id': 'exec', 'type': 'function', 'function': {
                    'name': 'exec', 'arguments': json.dumps({'command': command, 'timeout': 120})}}]}
        socket = self.root / 'worker.sock'
        with Broker(socket, Commands()):
            process = subprocess.Popen([sys.executable, '-m', 'conductor', '_worker', '--run', str(self.store.root),
                '--agent', 'a', '--socket', str(socket)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pid_file.exists(), 'Worker did not launch command')
                child_pid = int(pid_file.read_text())
                process.terminate()
                process.wait(timeout=5)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail('Shell child survived worker SIGTERM')
                self.assertEqual(self.store.agent('a')['state'], 'interrupted')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_background_children_do_not_outlive_a_finished_shell(self):
        self.store.configure(allow_exec=True)
        self.add('a')
        self.store.update_agent('a', state='running')
        import shlex
        code = 'import os,time; from pathlib import Path; Path("background.pid").write_text(str(os.getpid())); time.sleep(60)'
        command = shlex.join([sys.executable, '-c', code]) + ' & while [ ! -f background.pid ]; do sleep 0.05; done'
        result = AgentLoop(self.store, DemoProvider(), 'a').execute('exec', {'command': command, 'timeout': 5})
        self.assertEqual(result['exit_code'], 0)
        child_pid = int((self.repo / 'background.pid').read_text())
        for _ in range(40):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail('Background child survived shell completion')

    def test_filesystem_mode_reads_and_writes_outside_start_directory_without_git(self):
        self.store.configure(scope='filesystem', allow_exec=True)
        self.add('a')
        self.store.update_agent('a', state='running')
        outside = self.root / 'outside.txt'
        outside.write_text('original')
        loop = AgentLoop(self.store, DemoProvider(), 'a')
        self.assertEqual(loop.execute('read_file', {'path': str(outside)})['content'], 'original')
        self.assertIn('outside.txt', loop.execute('list_files', {'path': '..'}))
        import shlex
        result = loop.execute('exec', {'command': 'printf changed > ' + shlex.quote(str(outside))})
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(outside.read_text(), 'changed')
        with patch('conductor.runtime.git', side_effect=AssertionError('Filesystem mode must not call Git')):
            self.assertEqual(Scheduler(self.store, self.root / 'unused.sock').prepare(self.store.agent('a')), self.repo.resolve())
            result = loop.finish({'summary': 'Edited requested file directly.', 'outcome': 'done', 'findings': []})
        self.assertIsNone(result['patch'])

    def test_no_argument_chat_starts_in_home_and_keeps_prompted_key_out_of_state(self):
        import contextlib
        import io
        output = io.StringIO()
        def run(store, provider):
            self.assertEqual(store.config()['scope'], 'filesystem')
            self.assertTrue(store.config()['allow_exec'])
            self.assertEqual(store.config()['repo'], str(self.root.resolve()))
            self.assertIn('List my Downloads', [x['body'] for x in store.user_context()])
            store.update_agent('coordinator', state='done')
        with patch('conductor.cli.Path.home', return_value=self.root), \
             patch.dict(os.environ, {'OPENROUTER_API_KEY': ''}), \
             patch('conductor.cli.getpass.getpass', return_value='test-session-placeholder') as key, \
             patch('builtins.input', side_effect=['List my Downloads', '/exit']), \
             patch('conductor.cli.provider_for', return_value=DemoProvider()) as provider, \
             patch('conductor.cli.run_coordinator', side_effect=run), \
             patch('conductor.cli.signal.signal'), contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 0)
        key.assert_called_once()
        self.assertEqual(provider.call_args.args[1], 'test-session-placeholder')
        self.assertIn('Filesystem access', output.getvalue())
        for path in (self.root / '.local').rglob('*'):
            if path.is_file():
                self.assertNotIn(b'test-session-placeholder', path.read_bytes())


if __name__ == '__main__':
    unittest.main()
