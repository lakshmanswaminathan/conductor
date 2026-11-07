"""Fixture-only Pi trace projection and isolation tests; no user logs are read."""
import json
from pathlib import Path
import tempfile
import unittest

from conductor.pi_sessions import PiSessions, _clean


class PiSessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.log = self.root / 'project' / 'session.jsonl'
        self.log.parent.mkdir()
        self.records = [{'type': 'session', 'id': '11111111-2222-3333-4444-555555555555', 'version': 3, 'cwd': '/test/project'}]
        self.adapter = PiSessions(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, ident, parent, kind='message', **fields):
        entry = {'type': kind, 'id': ident, 'parentId': parent, 'timestamp': '2026-01-01T00:00:00Z', **fields}
        self.records.append(entry)
        return entry

    def message(self, ident, parent, role, content, **fields):
        return self.entry(ident, parent, message={'role': role, 'content': content, **fields})

    def save(self):
        self.log.write_text(''.join(json.dumps(r) + '\n' for r in self.records))

    def read(self, **kwargs):
        return self.adapter.read_session(self.records[0]['id'], **kwargs)

    def test_public_history_tools_secrets_and_branch_context(self):
        self.message('a', None, 'user', 'First prompt')
        self.entry('level', 'a', 'thinking_level_change', thinkingLevel='high')
        self.message('b', 'level', 'assistant', [
            {'type': 'thinking', 'thinking': 'PRIVATE_THOUGHT'},
            {'type': 'text', 'text': 'Public answer'},
            {'type': 'toolCall', 'id': 'call1', 'name': 'bash', 'arguments': {'command': 'echo safe', 'api_key': 'SENSITIVE_VALUE', 'GITHUB_TOKEN': 'NESTED_TOKEN_SECRET', 'nested': {'text': 'Bearer SECRET_TOKEN', 'data': 'useful'}}},
        ], model='example-model', usage={'input': 10, 'output': 2, 'cacheRead': 3})
        self.message('c', 'b', 'toolResult', [{'type': 'text', 'text': 'Failed'}], toolCallId='call1', toolName='bash', isError=True)
        self.message('old', 'c', 'user', 'Abandoned prompt')
        self.message('new', 'a', 'user', 'Branch prompt sk-or-v1-123456789abcdef')
        self.save()
        view = self.read()
        serialized = json.dumps(view)
        for forbidden in ('PRIVATE_THOUGHT', 'SENSITIVE_VALUE', 'NESTED_TOKEN_SECRET', 'SECRET_TOKEN', 'sk-or-v1-123456789abcdef'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(view['session']['prompt_count'], 3)
        self.assertEqual(view['session']['tool_count'], 1)
        self.assertEqual([x['id'] for x in view['context']['messages']], ['a', 'new'])
        tool = next(e for e in view['events'] if e['id'] == 'c')
        self.assertEqual(tool['tool_call_id'], 'call1'); self.assertTrue(tool['is_error'])
        historical = self.read(leaf_id='c')
        self.assertEqual([x['id'] for x in historical['context']['messages']], ['a', 'b', 'c'])
        self.assertFalse(historical['context']['leaf_inferred'])
        self.assertEqual(historical['session']['context_tokens'], 15)
        self.assertEqual(historical['session']['last_prompt'], 'First prompt')
        self.assertTrue(view['session']['last_prompt'].startswith('Branch prompt'))
        catalog = self.adapter.list_sessions(leaf_ids={self.records[0]['id']: 'old'})
        self.assertEqual(catalog['sessions'][0]['last_prompt'], 'Abandoned prompt')
        self.assertFalse(catalog['sessions'][0]['leaf_inferred'])
        self.assertIn('useful', json.dumps(historical))

    def test_compaction_checkpoint_edits_and_retain_none(self):
        self.message('sys', None, 'system', '', sections={'preamble': 'original'})
        self.message('a', 'sys', 'user', 'summarized')
        self.message('b', 'a', 'assistant', [{'type': 'text', 'text': 'kept original'}])
        self.entry('compact', 'b', 'compaction', firstKeptEntryId='b', summary='summary', tokensBefore=42,
                   systemMessage={'role': 'system', 'content': 'checkpoint'})
        self.entry('edit', 'compact', 'context_edit', targetId='b', replacement={'content': 'replacement'})
        self.message('c', 'edit', 'user', 'new prompt')
        self.save()
        view = self.read()
        self.assertEqual([x['id'] for x in view['context']['messages']], ['compact:system', 'compact', 'b', 'c'])
        self.assertEqual(next(x for x in view['context']['messages'] if x['id'] == 'b')['text'], 'replacement')
        self.assertEqual(next(x for x in view['events'] if x['id'] == 'b')['text'], 'kept original')
        self.entry('remove', 'c', 'context_edit', targetId='b', replacement=None)
        self.entry('compact2', 'remove', 'compaction', firstKeptEntryId='compact2', summary='new summary')
        self.save()
        self.assertEqual([x['id'] for x in self.read()['context']['messages']], ['compact2'])

    def test_incremental_partial_bad_records_pagination_and_rewrite(self):
        self.message('a', None, 'user', 'first'); self.save()
        with self.log.open('ab') as out:
            out.write(b'bad-json\n{"type":"message","id":"b",')
        first = self.read(); self.assertEqual(len(first['events']), 1)
        self.assertTrue(first['session']['partial_line']); self.assertEqual(first['session']['bad_records'], 1)
        cached = self.adapter._cache[self.log]
        with self.log.open('ab') as out:
            out.write(b'"parentId":"a","message":{"role":"user","content":"second"}}\n')
        page = self.read(limit=1)
        self.assertIs(cached, self.adapter._cache[self.log])
        self.assertEqual([x['id'] for x in page['events']], ['b'])
        self.assertEqual([x['id'] for x in self.read(before=page['next_before'])['events']], ['a'])
        self.assertFalse(page['session']['partial_line'])
        self.records = self.records[:1]; self.save()
        self.assertEqual(self.read()['events'], [])

    def test_collection_truncation_is_explicit_and_stable(self):
        value = _clean({'arguments': {f'field{i}': i for i in range(250)}, 'items': list(range(600))})
        self.assertIn('__truncated__', value['arguments'])
        self.assertEqual(value['items'][-1]['type'], 'truncation')
        self.assertEqual(_clean(value), value)
        self.message('a', None, 'user', [{'type': 'text', 'text': 'small'} for _ in range(600)])
        self.save()
        view = self.read()
        self.assertTrue(view['events'][0]['truncated'])
        self.assertTrue(view['truncated'])

    def test_root_isolation_and_bounds(self):
        self.message('a', None, 'user', 'x' * 100000); self.save()
        view = self.read()
        self.assertLess(len(json.dumps(view)), 250000)
        self.assertTrue(view['truncated'])
        with self.assertRaises(ValueError): self.adapter.read_session('../outside')
        with self.assertRaises(ValueError): self.read(leaf_id='missing')
        with self.assertRaises(ValueError): self.read(before=-1)
        outside = self.root.parent / (self.root.name + '-outside.jsonl')
        outside.write_text(json.dumps({'type': 'session', 'id': 'outside'}) + '\n')
        try:
            (self.root / 'link.jsonl').symlink_to(outside)
            self.assertEqual(len(self.adapter.list_sessions()['sessions']), 1)
        finally:
            outside.unlink()


if __name__ == '__main__':
    unittest.main()
