import json
import tempfile
import unittest
from pathlib import Path
from conductor.cli import initialize, parser
from conductor.runtime import AgentLoop


class TurnLimitTest(unittest.TestCase):
    def run_loop(self, limit, finish_at, cancel_at=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = initialize(root / 'run', root, 'test', max_turns=limit, ui=True)
            class Provider:
                calls = 0
                def complete(inner, messages, tools):
                    inner.calls += 1
                    if inner.calls == cancel_at:
                        store.configure(cancel_requested=True)
                    if inner.calls >= finish_at:
                        return {'tool_calls': [{'id': str(inner.calls), 'type': 'function', 'function': {
                            'name': 'finish', 'arguments': json.dumps({'summary': 'done', 'outcome': 'done', 'findings': []})}}]}
                    return {'content': 'continuing'}
            provider = Provider()
            try:
                AgentLoop(store, provider, 'coordinator').run()
                return provider.calls, store.agent('coordinator')['state']
            finally:
                store.close()

    def test_unlimited_runs_beyond_previous_default(self):
        self.assertEqual(self.run_loop(0, 65), (65, 'done'))

    def test_explicit_positive_limit_is_preserved(self):
        with self.assertRaisesRegex(RuntimeError, 'Agent turn ceiling reached'):
            self.run_loop(2, 3)

    def test_unlimited_still_obeys_cancellation(self):
        self.assertEqual(self.run_loop(0, 65, cancel_at=3)[0], 3)

    def test_cli_default_and_explicit_unlimited(self):
        self.assertEqual(parser().parse_args(['run', 'task']).max_turns, 0)
        self.assertEqual(parser().parse_args(['run', 'task', '--max-turns', '0']).max_turns, 0)
        with self.assertRaises(SystemExit):
            parser().parse_args(['run', 'task', '--max-turns', '-1'])
