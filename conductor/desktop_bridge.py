"""Durable user-chat relay. No provider, model loop, or execution capability."""
import hashlib
import json
from pathlib import Path
import re
import time

from . import codex_inbox
from .store import Store, encode


def _id(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValueError('Invalid ' + label)
    return value


def status(root):
    try:
        thread, _ = codex_inbox.target(Path(root).parent / 'swarm')
        return {'configured': True, 'thread_id': thread, 'execution': 'optional_pi'}
    except ValueError as exc:
        return {'configured': False, 'thread_id': None, 'execution': 'optional_pi', 'error': str(exc)}


def _store(root, chat_id, create=False):
    if not isinstance(chat_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', chat_id):
        raise ValueError('Invalid chat ID')
    path = Path(root) / chat_id
    if not create and not (path / 'state.sqlite').is_file():
        raise ValueError('Unknown bridge chat')
    store = Store(path)
    store.db.executescript('''
        CREATE TABLE IF NOT EXISTS bridge_requests(request_id TEXT PRIMARY KEY, fingerprint TEXT, event_id INTEGER, status TEXT, queue_id TEXT);
        CREATE TABLE IF NOT EXISTS bridge_replies(response_id TEXT PRIMARY KEY, fingerprint TEXT);
    ''')
    return store


def _config(db, values):
    db.executemany('INSERT OR REPLACE INTO config VALUES (?,?)', [(k, encode(v)) for k,v in values.items()])


def _state(store):
    rows = list(store.db.execute('SELECT status FROM bridge_requests ORDER BY event_id'))
    pending = [r['status'] for r in rows if r['status'] not in {'done', 'blocked', 'failed'}]
    state = ('delivery_unknown' if 'unknown' in pending else 'working' if 'working' in pending
             else 'awaiting_codex' if pending else rows[-1]['status'] if rows else 'pending')
    _config(store.db, {'bridge_pending': bool(pending)})
    store.db.execute("UPDATE agents SET state=? WHERE id='coordinator'", (state,))


def submit(root, message, request_id, chat_id=None):
    _id(request_id, 'request_id')
    if not isinstance(message, str) or not message.strip() or len(message) > 40000:
        raise ValueError('Enter a message of 1–40,000 characters')
    message = codex_inbox.clean_message(message.strip())
    swarm = Path(root).parent / 'swarm'
    thread, _ = codex_inbox.target(swarm)  # Fail closed before creating a chat.
    ident = chat_id or 'bridge-' + hashlib.sha256(request_id.encode()).hexdigest()[:24]
    fingerprint = hashlib.sha256(json.dumps([message, thread]).encode()).hexdigest()
    store = _store(root, ident, create=chat_id is None)
    try:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            prior = store.db.execute('SELECT * FROM bridge_requests WHERE request_id=?', (request_id,)).fetchone()
            if prior and prior['fingerprint'] != fingerprint:
                raise ValueError('Request ID belongs to different content or Codex target')
            if not prior:
                if not store.agent('coordinator'):
                    store.db.execute('INSERT INTO agents(id,parent,task,deps,state) VALUES(?,?,?,?,?)',
                                     ('coordinator', None, message, '[]', 'pending'))
                    _config(store.db, {'title': message[:100], 'model': 'Codex desktop', 'computer_access': True})
                event = store.db.execute('INSERT INTO events(source,target,kind,body,at) VALUES(?,?,?,?,?)',
                                         ('user', '*', 'instruction', message, time.time())).lastrowid
                store.db.execute('INSERT INTO bridge_requests VALUES(?,?,?,?,?)', (request_id, fingerprint, event, 'pending', None))
            _config(store.db, {'engine': 'bridge', 'allow_exec': False, 'ui': True, 'bridge_thread_id': thread})
            _state(store)
        config = json.loads((swarm / 'codex-target.json').read_text())
        context = {'chat_id': ident, 'request_id': request_id,
                   'coordinator_command': config.get('coordinator_command', 'scripts/conductor-pi-mcp')}
        delivery = codex_inbox.call(swarm, 'conductor-ui:' + ident, 'codex_send',
                                   {'message': message, 'request_id': request_id}, _ui_context=context)
        with store.db:
            # An immediate callback can arrive before queue returns; never regress it.
            store.db.execute("UPDATE bridge_requests SET status=?,queue_id=? WHERE request_id=? AND status IN ('pending','queued','unknown','failed')",
                             (delivery['status'], delivery.get('queue_id'), request_id))
            _config(store.db, {'bridge_delivery': delivery})
            _state(store)
        return {'id': ident, 'request_id': request_id, 'delivery': delivery, 'queued': delivery['status'] == 'queued'}
    finally:
        store.close()


def reply(root, args):
    allowed = {'chat_id', 'request_id', 'response_id', 'message', 'status'}
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError('Invalid reply arguments')
    request = _id(args.get('request_id'), 'request_id')
    response = _id(args.get('response_id'), 'response_id')
    message, state = args.get('message'), args.get('status', 'done')
    if not isinstance(message, str) or not message.strip() or len(message) > 40000 or state not in {'working', 'done', 'blocked'}:
        raise ValueError('Reply needs a message and working, done, or blocked status')
    message = codex_inbox.clean_message(message)
    store = _store(root, args.get('chat_id'))
    try:
        fingerprint = hashlib.sha256(json.dumps([request, message, state]).encode()).hexdigest()
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            original = store.db.execute('SELECT * FROM bridge_requests WHERE request_id=?', (request,)).fetchone()
            if not original:
                raise ValueError('Unknown request in this chat')
            prior = store.db.execute('SELECT fingerprint FROM bridge_replies WHERE response_id=?', (response,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('Response ID belongs to different content')
                return {'status': 'recorded', 'duplicate': True, 'response_id': response}
            if original['status'] in {'done', 'blocked'}:
                raise ValueError('This request already has a terminal reply')
            store.db.execute('INSERT INTO turns(agent,message,at) VALUES(?,?,?)',
                             ('coordinator', encode({'role':'assistant','content':message,'bridge_request_id':request}), time.time()))
            store.db.execute('INSERT INTO bridge_replies VALUES(?,?)', (response, fingerprint))
            store.db.execute('UPDATE bridge_requests SET status=? WHERE request_id=?', (state, request))
            _state(store)
        return {'status':'recorded', 'duplicate':False, 'response_id':response}
    finally:
        store.close()
