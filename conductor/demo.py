"""Deterministic end-to-end fixture. Never contacts a model provider."""
import json


class DemoProvider:
    usage = {}
    calls = 0

    def complete(self, messages, tools, max_tokens=4096):
        self.calls += 1
        if messages[0]['content'].startswith('Compact'):
            return {'role': 'assistant', 'content': 'Prior work archived. Preserve user instructions from the current snapshot. Original transcript remains retrievable by event ID.'}
        snapshot = next(m['content'] for m in messages if m.get('content', '').startswith('CURRENT CONTEXT SNAPSHOT'))
        state = json.loads(snapshot.split('\n', 1)[1])
        ident = state['task']['id']
        def call(name, **args):
            return {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'call-{self.calls}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
        if ident == 'coordinator':
            tasks = {a['id']: a for a in state['tasks']}
            for worker in ['research-a', 'research-b']:
                if worker not in tasks:
                    return call('dispatch', id=worker, task='Demo fixture: return one sourced context-store recommendation. No filesystem changes.')
            if 'review' not in tasks:
                return call('dispatch', id='review', task='Inspect both predecessor findings and report their unresolved disagreement.', dependencies=['research-a', 'research-b'])
            if any(t['state'] in {'pending', 'starting', 'running'} for k, t in tasks.items() if k != ident):
                return call('wait')
            bad = any(t['state'] != 'done' for k, t in tasks.items() if k != ident)
            return call('finish', summary='Three demo workers completed; both conflicting recommendations remain sourced in shared context. No live model was called.' if not bad else 'Demo workers failed; inspect status.', outcome='blocked' if bad else 'done', findings=[])
        if ident == 'review':
            conflicts = [f for f in state['findings'] if f['status'] == 'conflict']
            return call('finish', summary=f'Received both predecessor contexts; found {len(conflicts)} conflicting claims. User decision required.', outcome='done', findings=[{'key': 'review.context-store', 'value': 'Unresolved: research-a and research-b disagree; retain both.', 'evidence': ['research-a result', 'research-b result']}])
        return call('finish', summary='Demo recommendation; not a verified benchmark.', outcome='done', findings=[
            {'key': 'context.store', 'value': 'SQLite' if ident == 'research-a' else 'JSON files', 'evidence': [ident + ' fixture, not production evidence']}])
