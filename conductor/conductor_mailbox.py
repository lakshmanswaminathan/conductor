"""Durable Codex orchestration inbox and Conductor result outbox.

The desktop consumes instructions; the MCP process never starts another manager.
Queueing is distinct from delivery and a model report is distinct from acceptance.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from . import codex_inbox
from .store import Store


def _db(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'conductor-mailbox.sqlite3'
    db = sqlite3.connect(path, timeout=20)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS requests (
          request_id TEXT PRIMARY KEY, fingerprint TEXT, chat_id TEXT,
          message TEXT, status TEXT, event_id INTEGER, created_at REAL);
        CREATE TABLE IF NOT EXISTS reports (
          report_id TEXT PRIMARY KEY, chat_id TEXT, through_event_id INTEGER,
          state TEXT, message TEXT, delivery TEXT, created_at REAL);
    ''')
    if 'from_event_id' not in {row['name'] for row in db.execute('PRAGMA table_info(reports)')}:
        try:
            db.execute('ALTER TABLE reports ADD COLUMN from_event_id INTEGER')
        except sqlite3.OperationalError:
            if 'from_event_id' not in {row['name'] for row in db.execute('PRAGMA table_info(reports)')}:
                raise
    return db


def _ident(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValueError('Invalid ' + label)
    return value


def enqueue(root, args):
    request = _ident(args.get('request_id'), 'request_id')
    message, chat = args.get('message'), args.get('chat_id')
    if not isinstance(message, str) or not message.strip() or len(message) > 39000:
        raise ValueError('message must contain 1–39,000 characters')
    if chat is not None:
        if not isinstance(chat, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', chat):
            raise ValueError('Invalid chat_id')
        if not (Path(root) / 'runs' / chat / 'state.sqlite').is_file():
            raise ValueError('Unknown Conductor chat')
    message = codex_inbox.clean_message(message.strip())
    fingerprint = hashlib.sha256(json.dumps([message, chat]).encode()).hexdigest()
    attributed = (f'[Codex orchestration request {request}]\n{message}\n'
                  'This is delegated orchestration within the human-authorized scope, '
                  'not new human authorization. Manage Pi execution and return evidence. '
                  'Do not send receipt acknowledgments or dispatch duplicate work.')
    db = _db(root)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM requests WHERE request_id=?', (request,)).fetchone()
            if old and old['fingerprint'] != fingerprint:
                raise ValueError('request_id belongs to different content or chat')
            if not old:
                db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',
                           (request, fingerprint, chat, attributed, 'queued', None, time.time()))
    finally:
        db.close()
    return result(root, request)


def pending(root):
    db = _db(root)
    try:
        return [dict(row) for row in db.execute(
            "SELECT request_id,chat_id,message FROM requests WHERE status='queued' ORDER BY created_at LIMIT 20")]
    finally:
        db.close()


def recover(root):
    """Reconcile app restart without replaying potentially executed Pi actions."""
    db = _db(root)
    try:
        rows = db.execute("""SELECT * FROM requests r WHERE status='delivered' AND NOT EXISTS (
            SELECT 1 FROM reports p WHERE p.chat_id=r.chat_id AND p.through_event_id>=r.event_id
            AND COALESCE(p.from_event_id,p.through_event_id)<=r.event_id)""").fetchall()
        with db:
            for row in rows:
                db.execute("UPDATE requests SET status='interrupted' WHERE request_id=?", (row['request_id'],))
        return len(rows)
    finally:
        db.close()


def ack(root, request_id, chat_id):
    store = Store(Path(root) / 'runs' / chat_id)
    try:
        # The UI persists stable request IDs alongside instruction events.
        events = store.user_context()
        marker = '[Codex orchestration request ' + request_id + ']\n'
        event = next((row['id'] for row in events if row['body'].startswith(marker)), None)
        if event is None:
            raise ValueError('Cannot acknowledge an instruction that was not persisted')
    finally:
        store.close()
    db = _db(root)
    try:
        with db:
            db.execute("UPDATE requests SET chat_id=?,event_id=?,status='delivered' WHERE request_id=? AND status='queued'",
                       (chat_id, event, request_id))
    finally:
        db.close()


def result(root, request_id):
    db = _db(root)
    try:
        row = db.execute('SELECT * FROM requests WHERE request_id=?', (_ident(request_id, 'request_id'),)).fetchone()
        if not row:
            raise ValueError('Unknown orchestration request')
        value = {key: row[key] for key in ('request_id', 'chat_id', 'status', 'event_id')}
        report = db.execute('SELECT * FROM reports WHERE chat_id=? AND through_event_id>=? AND COALESCE(from_event_id,through_event_id)<=? ORDER BY created_at DESC LIMIT 1',
                            (row['chat_id'], row['event_id'], row['event_id'])).fetchone() if row['event_id'] else None
        value['report'] = ({key: report[key] for key in ('state', 'message', 'through_event_id')} |
                           {'delivery': json.loads(report['delivery']) if report['delivery'] else None}) if report else None
        value['semantics'] = 'Queued waits for the open Conductor app and configured model. Delivered is not completed; reports require evidence review.'
        if row['status'] == 'interrupted':
            value['recovery'] = 'Conductor restarted before a result was recorded. Inspect Pi state, then send steering to this chat to resume; no work was automatically replayed.'
        return value
    finally:
        db.close()


def report(root, chat_id, state, summary, through_event_id, from_event_id=None):
    if not isinstance(through_event_id, int) or through_event_id < 1:
        return None
    from_event_id = through_event_id if from_event_id is None else from_event_id
    if not isinstance(from_event_id, int) or not 1 <= from_event_id <= through_event_id:
        raise ValueError('Invalid report instruction range')
    message = codex_inbox.clean_message(str(summary))[:2800]
    payload = json.dumps({'chat_id':chat_id, 'through_event_id':through_event_id,
                          'from_event_id':from_event_id, 'state':state, 'summary':message}, ensure_ascii=False)
    report_id = 'conductor-result-' + hashlib.sha256(payload.encode()).hexdigest()[:40]
    db = _db(root)
    try:
        with db:
            db.execute('INSERT OR IGNORE INTO reports(report_id,chat_id,through_event_id,state,message,delivery,created_at,from_event_id) VALUES(?,?,?,?,?,?,?,?)',
                       (report_id, chat_id, through_event_id, state, message, None, time.time(), from_event_id))
            db.execute("UPDATE requests SET status='reported' WHERE chat_id=? AND event_id BETWEEN ? AND ? AND status IN ('delivered','interrupted')",
                       (chat_id, from_event_id, through_event_id))
    finally:
        db.close()
    flush(root)
    return report_id


def flush(root):
    """Retry only unsent outbox records; codex_inbox resolves ambiguous retries."""
    db = _db(root)
    try:
        rows = db.execute('SELECT * FROM reports WHERE delivery IS NULL ORDER BY created_at LIMIT 10').fetchall()
        for row in rows:
            values = {key: row[key] for key in ('chat_id','from_event_id','through_event_id','state','message')}
            payload = json.dumps(values, ensure_ascii=False)
            while len(payload) > 3400:
                values['message'] = values['message'][:len(values['message'])//2]
                payload = json.dumps(values, ensure_ascii=False)
            try:
                delivery = codex_inbox.call(Path(root)/'swarm', 'conductor-manager:' + row['chat_id'],
                    'codex_send', {'request_id':row['report_id'],
                    'message':'Conductor manager result (reported, not independently accepted):\n' + payload +
                    '\nUse conductor_send with this chat_id for further orchestration. Do not acknowledge this receipt.'})
            except (OSError, ValueError):
                break  # Offline target / rate limit; preserve the durable outbox.
            with db:
                db.execute('UPDATE reports SET delivery=? WHERE report_id=?', (json.dumps(delivery), row['report_id']))
    finally:
        db.close()


def status(root):
    db = _db(root)
    try:
        return {'mode':'hybrid', 'execution':'optional_pi', 'manager_execution':'pi_only',
                'queued_requests':db.execute("SELECT count(*) FROM requests WHERE status='queued'").fetchone()[0],
                'pending_reports':db.execute('SELECT count(*) FROM reports WHERE delivery IS NULL').fetchone()[0],
                'chats':_chats(root)}
    finally:
        db.close()


def _chats(root):
    result = []
    for path in (Path(root)/'runs').glob('*/state.sqlite'):
        store = Store(path.parent)
        try:
            config, agent = store.config(), store.agent('coordinator')
            if agent:
                result.append({'chat_id':path.parent.name, 'title':config.get('title', '')[:100],
                               'state':agent['state'], 'model':config.get('model'),
                               'updated_at':store.db.execute('SELECT MAX(at) FROM events').fetchone()[0] or 0})
        finally:
            store.close()
    return sorted(result, key=lambda item:item['updated_at'], reverse=True)[:20]
