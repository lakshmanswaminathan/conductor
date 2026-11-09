"""Durable manager notification identities, separate from transport receipts."""
import hashlib
import json
import re
import sqlite3

FIELDS = {
    'notification_kind': {'type': 'string', 'enum': ['progress', 'failure', 'escalation', 'final']},
    'task_key': {'type': 'string', 'minLength': 1, 'maxLength': 256},
    'checkpoint': {'type': 'string', 'minLength': 1, 'maxLength': 256},
    'progress_cursor': {'type': 'string', 'minLength': 1, 'maxLength': 256},
}
SEMANTICS = 'Queued is transport delivery only; it does not mean the report was processed, accepted, or the task completed.'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def send(store, root, actor, args, transport):
    from .codex_inbox import clean_message, target
    if set(args) - {'message', 'request_id', *FIELDS}:
        raise ValueError('Unknown inbox argument')
    request = args.get('request_id')
    if not isinstance(request, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request):
        raise ValueError('request_id must contain 1–128 letters, digits, or _ . : -')
    for field in FIELDS:
        if field in args and (not isinstance(args[field], str) or not args[field].strip() or len(args[field]) > 256):
            raise ValueError(f'{field} must contain 1–256 characters')
    kind = args.get('notification_kind')
    if kind is not None and kind not in FIELDS['notification_kind']['enum']:
        raise ValueError('Unknown notification_kind')
    if kind == 'progress' and not all(args.get(key) for key in ('task_key', 'checkpoint')):
        raise ValueError('Progress notifications require task_key and checkpoint')
    thread, _ = target(root)
    message = clean_message(args['message'])
    metadata = {key: args[key] for key in FIELDS if key in args}
    fingerprint = _hash([thread, message, metadata])
    content = fingerprint
    checkpoint = (_hash([thread, args['task_key'], args['checkpoint'], args.get('progress_cursor')])
                  if kind == 'progress' else None)
    path = store.root / 'codex-notifications.sqlite3'
    db = sqlite3.connect(path, timeout=20)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE IF NOT EXISTS notifications(request_id TEXT PRIMARY KEY, fingerprint TEXT, content TEXT, checkpoint TEXT, canonical TEXT, receipt TEXT)')
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM notifications WHERE request_id=?', (request,)).fetchone()
            if row and row['fingerprint'] != fingerprint:
                raise ValueError('request_id already belongs to different content or target')
            if not row:
                row = db.execute('SELECT * FROM notifications WHERE content=? OR (checkpoint IS NOT NULL AND checkpoint=?) ORDER BY rowid LIMIT 1', (content, checkpoint)).fetchone()
                if row and row['content'] != content:
                    raise ValueError('Checkpoint already has different content; advance checkpoint or progress_cursor for a new report')
                if row:
                    db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,NULL)', (request, fingerprint, content, checkpoint, row['canonical']))
            if row:
                canonical = row['canonical']
                saved = db.execute('SELECT receipt FROM notifications WHERE request_id=?', (canonical,)).fetchone()
                result = json.loads(saved['receipt'])
                if 'validation_error' in result:
                    raise ValueError(result['validation_error'])
                if not result.get('retryable_rate_limit'):
                    return {**result, 'deduplicated': True, 'notification_request_id': request, 'semantics': SEMANTICS}
                request = canonical
            # An interrupted/ambiguous transport invocation must never be replayed,
            # including under a new request ID or a reworded progress report.
            pending = {'request_id': request, 'thread_id': thread, 'status': 'unknown', 'queue_id': None}
            if row:
                db.execute('UPDATE notifications SET receipt=? WHERE request_id=?', (json.dumps(pending), request))
            else:
                db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,?)', (request, fingerprint, content, checkpoint, request, json.dumps(pending)))
        try:
            result = transport(root, actor, 'codex_send', {'request_id': request, 'message': args['message']})
        except ValueError as exc:
            # Only the transport's known pre-enqueue rate limit is retryable.
            # Keep request fingerprints even then, so changed retries conflict.
            error = ({'retryable_rate_limit': True} if str(exc) == 'Codex inbox rate limit: 10 messages per minute'
                     else {'validation_error': str(exc)})
            with db:
                db.execute('UPDATE notifications SET receipt=? WHERE request_id=?', (json.dumps(error), request))
            raise
        with db:
            db.execute('UPDATE notifications SET receipt=? WHERE request_id=?', (json.dumps(result), request))
        return {**result, 'semantics': SEMANTICS}
    finally:
        db.close()
