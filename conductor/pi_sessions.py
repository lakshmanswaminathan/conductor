"""Read-only, public projections of Pi's append-only session trees (format v3)."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import threading
from datetime import datetime, timezone

_PRIVATE = re.compile(r'thinking|reasoning|signature', re.I)
_SECRET_KEY = re.compile(r'^(?:(?:.*[_-])?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|credential)s?|(?:.*[_-])?token)$', re.I)
_SECRETS = [
    re.compile(r'\b(?:sk-(?:or-v1-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}'),
    re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
    re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+'),
    re.compile(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[=:]\s*[\"\']?)[^\s\"\'&,;]+'),
    re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)'),
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value, depth=0):
    if depth > 20:
        return '[truncated]'
    if isinstance(value, str):
        for pattern in _SECRETS:
            value = pattern.sub('[redacted]', value)
        return value[:16000] + (' [truncated]' if len(value) > 16000 else '')
    if isinstance(value, dict):
        if _PRIVATE.search(str(value.get('type', ''))):
            return None
        result = {str(k): '[redacted]' if _SECRET_KEY.match(str(k)) else _clean(v, depth + 1)
                  for k, v in list(value.items())[:199 if len(value) > 200 else 200] if not _PRIVATE.search(str(k))}
        if len(value) > 200:
            result['__truncated__'] = '[truncated] additional object fields omitted'
        return result
    if isinstance(value, list):
        result = [clean for v in value[:499 if len(value) > 500 else 500] if (clean := _clean(v, depth + 1)) is not None]
        if len(value) > 500:
            result.append({'type': 'truncation', 'text': '[truncated] additional array items omitted', 'omitted_items': len(value) - 499})
        return result
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (int, float, bool, type(None))) else None


def _content(value):
    if isinstance(value, str):
        return _clean(value)
    if not isinstance(value, list):
        return []
    blocks = []
    for block in value:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind in ('text', 'truncation'):
            blocks.append({'type': 'text', 'text': _clean(str(block.get('text', '')))})
        elif kind == 'toolCall':
            blocks.append({'type': 'toolCall', 'id': block.get('id'), 'name': block.get('name'),
                           'arguments': _clean(block.get('arguments', {}))})
        elif kind in ('image', 'audio'):
            blocks.append({'type': kind, 'omitted': True})
    return blocks


def _task_preview(text):
    if text.startswith(('[Task delegated by the user', 'Conductor delegated task ')):
        try:
            payload = json.loads(text.split('\n', 2)[1])
            if isinstance(payload.get('task'), str):
                return _clean(payload['task'])[:240]
        except (ValueError, IndexError, AttributeError):
            pass
    return text[:240]


def _bounded(value, maximum=18000):
    result = _clean(value)
    if '[truncated]' in json.dumps(result):
        result['truncated'] = True
    if len(json.dumps(result, ensure_ascii=True)) <= maximum:
        return result
    # Keep a structured identity and a bounded public preview, never partial JSON.
    keep = {k: result[k] for k in ('id', 'type', 'parent_id', 'timestamp', 'role', 'tool_call_id', 'tool_name', 'is_error') if k in result}
    preview = json.dumps(result, ensure_ascii=True)[:maximum // 3]
    keep.update(text=preview, truncated=True)
    return keep


def _event(entry):
    event = {key: entry.get(source) for key, source in [('id', 'id'), ('type', 'type'), ('parent_id', 'parentId'), ('timestamp', 'timestamp')]}
    kind = entry.get('type')
    if kind == 'message':
        message = entry.get('message')
        if not isinstance(message, dict):
            return None
        role = message.get('role')
        if role not in ('user', 'assistant', 'toolResult', 'system', 'custom'):
            return None
        content = _content(message.get('content'))
        event.update(role=role, content=content,
                     text=content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content if b.get('type') == 'text'))
        if role == 'assistant':
            event['tool_calls'] = [b for b in content if b.get('type') == 'toolCall'] if isinstance(content, list) else []
            event['model'] = message.get('model')
            event['usage'] = _clean(message.get('usage', {}))
        if role == 'toolResult':
            event.update(tool_call_id=message.get('toolCallId'), tool_name=message.get('toolName'), is_error=bool(message.get('isError')))
        if role == 'system':
            for key in ('sections', 'toolsAdded', 'toolsRemoved'):
                if key in message:
                    event[key] = _clean(message[key])
    elif kind in ('compaction', 'branch_summary'):
        event.update(text=entry.get('summary', ''), first_kept_entry_id=entry.get('firstKeptEntryId'), tokens_before=entry.get('tokensBefore'), from_id=entry.get('fromId'))
    elif kind == 'context_edit':
        replacement = entry.get('replacement')
        event.update(target_id=entry.get('targetId'), replacement=None if replacement is None else _content(replacement.get('content') if isinstance(replacement, dict) else replacement))
    elif kind == 'custom_message':
        content = _content(entry.get('content'))
        event.update(role='custom', content=content, custom_type=entry.get('customType'), display=entry.get('display'),
                     text=content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content if b.get('type') == 'text'))
    elif kind == 'model_change':
        event.update(model=entry.get('modelId'), provider=entry.get('provider'))
    elif kind in ('session_info', 'label'):
        event.update(name=entry.get('name'), label=entry.get('label'), target_id=entry.get('targetId'))
    elif kind == 'usage':
        event.update(model=entry.get('model'), usage=_clean(entry.get('usage', {})))
    else:
        return None
    if entry.get('truncated'):
        event['truncated'] = True
    return _bounded(event)


class PiSessions:
    def __init__(self, root=None):
        self.root = Path(root or Path.home() / '.pi/agent/sessions').expanduser().resolve()
        self._lock = threading.RLock()
        self._cache = {}
        self._sources = {}

    def set_sources(self, records):
        """Explicit observer registrations, never a filesystem-wide log scan."""
        with self._lock:
            self._sources = dict(records)

    def _load(self, path):
        stat = path.stat()
        old = self._cache.get(path)
        fingerprint = (stat.st_dev, stat.st_ino)
        if old and old['fingerprint'] == fingerprint and old['size'] == stat.st_size and old['mtime'] == stat.st_mtime_ns:
            return old
        if not old or old['fingerprint'] != fingerprint or stat.st_size < old['size'] or (stat.st_size == old['size'] and stat.st_mtime_ns != old['mtime']):
            old = {'fingerprint': fingerprint, 'offset': 0, 'header': None, 'entries': [], 'bad_records': 0}
        with path.open('rb') as source:
            source.seek(old['offset'])
            while True:
                start = source.tell()
                line = source.readline()
                if not line or not line.endswith(b'\n'):
                    old['offset'] = start
                    break
                old['offset'] = source.tell()
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError('not an object')
                    if record.get('type') == 'session' and old['header'] is None:
                        identifier = record.get('id')
                        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
                            raise ValueError('invalid session id')
                        old['header'] = _clean(record)
                    elif isinstance(record.get('id'), str) and isinstance(record.get('type'), str) and (record.get('parentId') is None or isinstance(record.get('parentId'), str)):
                        cleaned = _clean(record)
                        old['entries'].append(cleaned if isinstance(cleaned, dict) else {k: record.get(k) for k in ('id', 'parentId', 'type', 'timestamp')})
                    else:
                        old['bad_records'] += 1
                except (ValueError, UnicodeError, RecursionError):
                    old['bad_records'] += 1
        old.update(size=stat.st_size, mtime=stat.st_mtime_ns, session_file=str(path), updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), partial_line=old['offset'] < stat.st_size)
        self._cache[path] = old
        return old

    def _discover(self):
        found = {}
        paths = set()
        candidates = {p: None for p in self.root.rglob('*.jsonl')} if self.root.is_dir() else {}
        for ident, record in self._sources.items():
            name = record.get('session_file')
            if isinstance(name, str) and Path(name).is_absolute() and Path(name).suffix == '.jsonl':
                candidates[Path(name)] = ident
        for path, expected_id in candidates.items():
            try:
                if path.is_symlink() or not path.is_file() or (expected_id is None and not path.resolve().is_relative_to(self.root)):
                    continue
                paths.add(path)
                state = self._load(path)
                if state['header']:
                    ident = state['header']['id']
                    if expected_id is not None and ident != expected_id:
                        continue
                    if ident not in found or state['mtime'] > found[ident]['mtime']:
                        found[ident] = state
            except OSError:
                continue
        for ident, record in self._sources.items():
            if ident in found:
                continue
            # Print/RPC and --no-session agents can have no durable JSONL yet.
            # Only use observer entries, and label them as a bounded snapshot.
            entries = [e for e in record.get('recent_entries', []) if isinstance(e, dict)
                       and isinstance(e.get('id'), str) and isinstance(e.get('type'), str)]
            if not entries and not record.get('observed_at'):
                continue
            at = record.get('observed_at', 0)
            found[ident] = {'header': {'id': ident, 'cwd': record.get('cwd', ''),
                                     'parentSession': record.get('parent_session_file')},
                            'entries': entries, 'mtime': int(at * 1e9),
                            'updated_at': datetime.fromtimestamp(at, timezone.utc).isoformat(),
                            'bad_records': 0, 'partial_line': False, 'session_file': record.get('session_file'),
                            'snapshot_only': True, 'snapshot_truncated': record.get('recent_entries_truncated', False)}
        self._cache = {p: state for p, state in self._cache.items() if p in paths}
        return found

    @staticmethod
    def _path(entries, leaf_id=None):
        index = {e['id']: e for e in entries}
        if leaf_id is not None and leaf_id not in index:
            raise ValueError('Unknown leaf_id')
        current = index.get(leaf_id) if leaf_id is not None else (entries[-1] if entries else None)
        path, seen = [], set()
        while current and current['id'] not in seen:
            seen.add(current['id']); path.append(current)
            current = index.get(current.get('parentId'))
        return list(reversed(path))

    def _metadata(self, state, leaf_id=None):
        entries = state['entries']; path = self._path(entries, leaf_id)
        prompts = [e['message'] for e in entries if e.get('type') == 'message' and isinstance(e.get('message'), dict) and e['message'].get('role') == 'user']
        active_prompts = [e['message'] for e in path if e.get('type') == 'message' and isinstance(e.get('message'), dict) and e['message'].get('role') == 'user']
        last = _content(active_prompts[-1].get('content')) if active_prompts else ''
        if isinstance(last, list):
            last = '\n'.join(b.get('text', '') for b in last)
        model = None; tokens = None
        for entry in path:
            msg = entry.get('message', {})
            if entry['type'] == 'model_change':
                model = entry.get('modelId')
            elif isinstance(msg, dict) and msg.get('role') == 'assistant':
                model = msg.get('model', model)
                usage = msg.get('usage', {})
                if isinstance(usage, dict):
                    parts = [usage.get(k, 0) for k in ('input', 'output', 'cacheRead', 'cacheWrite')]
                    if all(isinstance(x, (float, int)) for x in parts) and any(parts):
                        tokens = sum(parts)
        tool_count = sum(len([b for b in e.get('message', {}).get('content', []) if isinstance(b, dict) and b.get('type') == 'toolCall']) for e in entries if isinstance(e.get('message'), dict) and isinstance(e['message'].get('content'), list))
        result = {'id': state['header']['id'], 'cwd': state['header'].get('cwd', ''), 'updated_at': state['updated_at'], 'model': model, 'prompt_count': len(prompts), 'tool_count': tool_count, 'counts_scope': 'all branches; bounded public records', 'active_prompt_count': len(active_prompts), 'leaf_id': path[-1]['id'] if path else None, 'leaf_inferred': leaf_id is None, 'last_prompt': last[:1000], 'last_prompt_truncated': len(last) > 1000 or '[truncated]' in last, 'entry_count': len(entries), 'bad_records': state['bad_records'], 'partial_line': state['partial_line']}
        if tokens is not None:
            result.update(context_tokens=tokens, context_tokens_source='latest assistant usage; approximate, not live context measurement')
        result['last_prompt_id'] = next((e['id'] for e in reversed(path) if e.get('type') == 'message' and isinstance(e.get('message'), dict) and e['message'].get('role') == 'user'), None)
        result['task_preview'] = _task_preview(last)
        # Public, bounded evidence for the overview; never summarize private reasoning
        # or scan another branch to infer what the active session is doing.
        recent = []
        for entry in path[-12:]:
            event = _event(entry)
            if not event or event.get('role') not in {'user', 'assistant', 'toolResult'}:
                continue
            message = entry.get('message', {})
            recent.append({
                'id':event.get('id'), 'timestamp':event.get('timestamp'), 'role':event['role'],
                'text':str(event.get('text') or (_clean(message.get('errorMessage')) if message.get('stopReason') == 'error' else '') or '')[:400], 'tool_name':event.get('tool_name'),
                'is_error':bool(event.get('is_error') or message.get('stopReason') == 'error'),
                'tool_names':[call.get('name') for call in event.get('tool_calls', [])[:4]],
            })
        result['recent_activity'] = recent
        result.update(session_file=state.get('session_file'), parent_session_file=state['header'].get('parentSession'),
                      trace_source='observer_snapshot' if state.get('snapshot_only') else 'session_file')
        return _clean(result)

    def list_sessions(self, limit=50, leaf_ids=None, offset=0, preferred_ids=()):
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        if leaf_ids is not None and (not isinstance(leaf_ids, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in leaf_ids.items())):
            raise ValueError('leaf_ids must map session identifiers to leaf identifiers')
        with self._lock:
            states = sorted(self._discover().values(), key=lambda s: s['mtime'], reverse=True)
            states.sort(key=lambda s: s['header']['id'] not in preferred_ids)
            sessions = []
            for state in states[offset:offset + limit]:
                leaf = (leaf_ids or {}).get(state['header']['id'])
                try:
                    metadata = self._metadata(state, leaf)
                except ValueError:
                    metadata = self._metadata(state)
                    metadata['leaf_override_missing'] = True
                sessions.append(metadata)
            return {'sessions': sessions, 'observed_at': _now(), 'truncated': len(states) > offset + limit,
                    'total_sessions': len(states), 'next_offset': offset + limit if len(states) > offset + limit else None}

    def read_session(self, session_id, limit=100, before=None, leaf_id=None):
        limit = max(1, min(int(limit), 300))
        if not isinstance(session_id, str) or (leaf_id is not None and not isinstance(leaf_id, str)):
            raise ValueError('Session and leaf identifiers must be strings')
        with self._lock:
            state = self._discover().get(session_id)
            if state is None:
                raise ValueError('Unknown Pi session')
            entries = state['entries']; path = self._path(entries, leaf_id)
            end = len(entries) if before is None else int(before)
            if end < 0 or end > len(entries):
                raise ValueError('Invalid before cursor')
            events, budget, cursor = [], 0, end
            while cursor > 0 and len(events) < limit:
                event = _event(entries[cursor - 1])
                if event:
                    size = len(json.dumps(event))
                    if budget + size > 120000:
                        break
                    events.append(event); budget += size
                cursor -= 1
            events.reverse()
            compactions = [i for i, e in enumerate(path) if e['type'] == 'compaction']
            compaction = None; selected = path
            if compactions:
                ci = compactions[-1]; compaction = path[ci]
                first = next((i for i, e in enumerate(path[:ci]) if e['id'] == compaction.get('firstKeptEntryId')), ci)
                kept = [e for e in path[first:ci] if not (e['type'] == 'message' and isinstance(e.get('message'), dict) and e['message'].get('role') == 'system')]
                selected = [compaction] + kept + path[ci + 1:]
            edits = {e['targetId']: e for e in selected if e['type'] == 'context_edit' and isinstance(e.get('targetId'), str)}
            messages = []
            for i, source in enumerate(selected):
                if source['type'] not in ('message', 'custom_message', 'compaction', 'branch_summary') or (source['type'] == 'compaction' and i > 0):
                    continue
                entry = copy.deepcopy(source)
                edit = edits.get(entry['id'])
                editable = entry['type'] == 'custom_message' or (entry['type'] == 'message' and isinstance(entry.get('message'), dict) and entry['message'].get('role') in ('user', 'assistant', 'toolResult', 'custom'))
                if edit and editable:
                    replacement = edit.get('replacement')
                    if replacement is None:
                        continue
                    content = replacement.get('content') if isinstance(replacement, dict) else replacement
                    if entry['type'] == 'message' and isinstance(entry.get('message'), dict):
                        entry['message']['content'] = content
                    else:
                        entry['content'] = content
                if source['type'] == 'compaction' and isinstance(source.get('systemMessage'), dict):
                    checkpoint = _event({'type': 'message', 'id': source['id'] + ':system', 'message': source['systemMessage'], 'timestamp': source.get('timestamp')})
                    if checkpoint:
                        messages.append(checkpoint)
                event = _event(entry)
                if event:
                    messages.append(event)
            # Prefer the most recent context when the public view must be bounded.
            bounded, budget = [], 0
            for message in reversed(messages):
                size = len(json.dumps(message))
                if budget + size > 100000 or len(bounded) >= 200:
                    break
                bounded.append(message); budget += size
            bounded.reverse()
            limitations = ['Public log reconstruction only: thinking/private reasoning and media payloads are excluded; credentials are redacted.', 'The current Pi leaf is not persisted on navigation alone; latest appended entry is inferred unless leaf_id is provided.', 'System entries contain prompt/tool patches; this is a provenance view, not an exact serialized provider request.']
            if state.get('snapshot_only'):
                limitations.append('Observer snapshot only; durable session log unavailable. Earlier history may be missing.')
            context = {'messages': bounded, 'compaction': _event(compaction) if compaction else None, 'leaf_id': path[-1]['id'] if path else None, 'leaf_inferred': leaf_id is None, 'limitations': limitations, 'truncated': len(bounded) < len(messages), 'message_count': len(messages)}
            context['truncated'] |= bool(state.get('snapshot_truncated'))
            return {'session': self._metadata(state, leaf_id), 'events': events, 'context': context, 'next_before': str(cursor) if cursor else None, 'observed_at': _now(), 'truncated': any(e.get('truncated') for e in events) or context['truncated']}
