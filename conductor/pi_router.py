"""Codex coordinates; Pi executes. No local data or environment execution path."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from . import codex_inbox
from .pi_monitor import monitor, context_digest
from .swarm import SwarmBroker


def _db(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'pi-routes.sqlite3'
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    path.chmod(0o600)
    db.execute('''CREATE TABLE IF NOT EXISTS routes(
        request_id TEXT PRIMARY KEY,fingerprint TEXT,session_id TEXT,instance_id TEXT,
        task_id TEXT,spec TEXT,delivery TEXT,review TEXT,created_at REAL)''')
    db.execute("""CREATE TABLE IF NOT EXISTS controls(
        control_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        delivery TEXT NOT NULL, created_at REAL NOT NULL)""")
    return db


def _text(value, field, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{field} must be nonempty text, at most {maximum} characters')
    return value


def _enabled(root):
    path = Path(root).parent / 'ui-settings.json'
    if path.exists() and not json.loads(path.read_text()).get('computer_access', True):
        raise ValueError('Pi execution is disabled in Conductor Settings')


def dispatch(root, args):
    _enabled(root)
    allowed = {'request_id','session_id','instance_id','operation','task','ownership','acceptance'}
    if not isinstance(args, dict) or set(args) != allowed:
        raise ValueError('Dispatch requires request_id, exact session/instance, operation, task, ownership, and acceptance')
    request = _text(args['request_id'], 'request_id')
    for key in ('request_id','session_id','instance_id'):
        _text(args[key], key)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args[key]):
            raise ValueError('Invalid ' + key)
    if args['operation'] not in {'data_write','environment','other'}:
        raise ValueError('Choose data_write, environment, or other; all execute in Pi')
    _text(args['task'], 'task', 4000)
    for key in ('ownership','acceptance'):
        if not isinstance(args[key], list) or not 1 <= len(args[key]) <= 20:
            raise ValueError(key + ' needs 1–20 explicit criteria')
        for value in args[key]:
            _text(value, key, 300)
    spec = {key: args[key] for key in sorted(allowed)}
    fingerprint = hashlib.sha256(json.dumps(spec).encode()).hexdigest()
    task_id = 'codex-pi-' + hashlib.sha256(request.encode()).hexdigest()[:24]
    payload = ('[Task delegated by the user’s Codex orchestrator through Conductor]\n' +
               json.dumps({'task_id':task_id,'operation':args['operation'],'task':args['task'],
                           'ownership':args['ownership'],'acceptance':args['acceptance']}, ensure_ascii=False) +
               '\nYou own execution, including data writes, environment operations, and verification. '
               'Stay within the assigned scope and existing user approvals. '
               'Use swarm_task to claim this exact task, then update it to completed with verification '
               'evidence, or blocked/failed with the reason. Conductor relays those updates back to Codex. '
               'Do not send receipt-only messages or unrelated work. Reported completion needs Codex review.')
    if len(payload) > 12000:
        raise ValueError('Assignment exceeds Pi message size limit')
    db = _db(root)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM routes WHERE request_id=?', (request,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('Request ID belongs to a different Pi assignment')
                return result(root, request)
            bridge = monitor.bridges().get(args['session_id'])
            if not bridge or bridge.get('instance_id') != args['instance_id'] or not monitor.control_path(bridge):
                raise ValueError('Exact live Pi session/instance with control attachment required; no local fallback')
            db.execute('INSERT OR IGNORE INTO routes VALUES(?,?,?,?,?,?,?,?,?)',
                       (request, fingerprint, args['session_id'], args['instance_id'], task_id,
                        json.dumps(spec), json.dumps({'status':'unknown'}), None, time.time()))
        broker = SwarmBroker(root, monitor)
        try:
            broker.call('conductor','swarm_task', {'action':'create','task_id':task_id,
                'title':args['task'][:300], 'description':args['task'], 'owner':args['session_id'],
                'request_id':task_id+'-create'})
            delivery = monitor.command(args['session_id'], args['instance_id'], task_id, 'follow_up', payload)
        finally:
            broker.close()
        with db:
            db.execute('UPDATE routes SET delivery=? WHERE request_id=?', (json.dumps(delivery), request))
    finally:
        db.close()
    return result(root, request)


def result(root, request):
    _text(request, 'request_id')
    db = _db(root)
    try:
        row = db.execute('SELECT * FROM routes WHERE request_id=?', (request,)).fetchone()
        if not row:
            raise ValueError('Unknown Pi assignment')
        item = dict(row)
    finally:
        db.close()
    broker = SwarmBroker(root, monitor)
    try:
        with broker.lock:
            task = broker.db.execute('SELECT * FROM tasks WHERE task_id=?', (item['task_id'],)).fetchone()
            task = dict(task) if task else None
    finally:
        broker.close()
    if task and task['owner'] != item['session_id']:
        raise ValueError('Pi assignment ownership changed; inspect before continuing')
    reviewed = json.loads(item['review']) if item['review'] else None
    return {'request_id':request,'session_id':item['session_id'],'instance_id':item['instance_id'],
            'task_id':item['task_id'],'operation':json.loads(item['spec'])['operation'],
            'delivery':json.loads(item['delivery']),'task':task,
            'reconciliation':('Delivery is uncertain. Inspect the exact Pi instance and task before further action; this request will not be replayed.' if json.loads(item['delivery']).get('status') == 'unknown' else None),
            'review':reviewed,
            'complete':bool(reviewed and reviewed['accepted'] and task and task['status']=='completed'
                            and reviewed.get('reported_result') == task['result'])}


def control(root, args):
    """Steer the existing owner without creating or reassigning a task."""
    _enabled(root)
    if not isinstance(args, dict) or set(args) != {'request_id', 'control_id', 'action', 'message'}:
        raise ValueError('Control requires request_id, control_id, action, and message')
    for key in ('request_id', 'control_id'):
        if not isinstance(args[key], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args[key]):
            raise ValueError('Invalid ' + key)
    if args['action'] not in {'steer', 'follow_up'}:
        raise ValueError('Control action must be steer or follow_up')
    _text(args['message'], 'message', 4000)
    fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    db = _db(root)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM controls WHERE control_id=?', (args['control_id'],)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('Control ID belongs to different content or assignment')
                delivery = json.loads(previous['delivery'])
            else:
                current = result(root, args['request_id'])
                if current['delivery'].get('status') != 'accepted':
                    raise ValueError('Assignment delivery is not accepted; reconcile the exact owner before control')
                task = current['task']
                if not task or task['status'] in {'completed', 'blocked', 'failed'}:
                    raise ValueError('No active owner assignment; terminal reports require review and a new assignment')
                bridge = monitor.bridges().get(current['session_id'])
                if not bridge or bridge.get('instance_id') != current['instance_id'] or not monitor.control_path(bridge):
                    raise ValueError('Exact live Pi owner instance required; reconcile stale instance before control')
                delivery = {'status': 'unknown'}
                db.execute('INSERT INTO controls VALUES(?,?,?,?,?)',
                           (args['control_id'], args['request_id'], fingerprint, json.dumps(delivery), time.time()))
        if not previous:
            command_id = 'codex-control-' + hashlib.sha256(args['control_id'].encode()).hexdigest()[:24]
            payload = ('Existing assignment control; preserve the original ownership, acceptance criteria, '
                       'scope and approvals. Do not create another task.\n' +
                       json.dumps({'task_id': current['task_id'], 'message': args['message']}, ensure_ascii=False))
            delivery = monitor.command(current['session_id'], current['instance_id'], command_id, args['action'], payload)
            with db:
                db.execute('UPDATE controls SET delivery=? WHERE control_id=?', (json.dumps(delivery), args['control_id']))
        return {'request_id': args['request_id'], 'control_id': args['control_id'], 'delivery': delivery,
                'reconciliation': ('Delivery is uncertain. Inspect the exact owner and task; this control will not be replayed.'
                                   if delivery.get('status') == 'unknown' else None)}
    finally:
        db.close()


def review(root, args):
    request = _text(args.get('request_id'), 'request_id')
    evidence = args.get('evidence')
    if type(args.get('accepted')) is not bool or not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise ValueError('Review needs accepted and 1–20 evidence references')
    for entry in evidence:
        _text(entry, 'evidence', 1000)
    current = result(root, request)
    if not current['task'] or current['task']['status'] not in {'completed','blocked','failed'}:
        raise ValueError('Pi has not reported a terminal result')
    if args['accepted'] and current['task']['status'] != 'completed':
        raise ValueError('Only a reported completed task can be accepted')
    record = {'accepted':args['accepted'],'evidence':evidence,'reported_result':current['task']['result'],'reported_status':current['task']['status']}
    db = _db(root)
    try:
        with db:
            db.execute('UPDATE routes SET review=? WHERE request_id=?', (json.dumps(record), request))
    finally:
        db.close()
    return result(root, request)


def relay_results(root):
    """Called by the local broker, not a model. Unchanged results queue once."""
    if not (Path(root) / 'pi-routes.sqlite3').exists():
        return
    _enabled(root)
    db = _db(root)
    try:
        requests = [r[0] for r in db.execute('SELECT request_id FROM routes')]
    finally:
        db.close()
    for request in requests:
        receipt = result(root, request)
        task = receipt['task']
        if not task or task['status'] not in {'completed','blocked','failed'}:
            continue
        reviewed = receipt['review']
        if reviewed and reviewed.get('reported_result') == task['result'] and reviewed.get('reported_status') == task['status']:
            continue
        data = {'request_id':request,'session_id':receipt['session_id'],'task_id':receipt['task_id'],
                'status':task['status'],'result':task['result']}
        key = 'pi-result-' + hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:32]
        message = 'Pi reported a task result. Review evidence before accepting; do not execute environment or data-write work locally.\n' + json.dumps(data, ensure_ascii=False)
        # A large report stays on the task board and can be fetched with pi_result.
        if len(message) > 4000:
            data['result'] = str(task['result'])[:2200] + ' [read full result with pi_result]'
            message = 'Pi result needs Codex review.\n' + json.dumps(data, ensure_ascii=False)
        codex_inbox.call(root, 'pi-router', 'codex_send', {'request_id':key,'message':message})


def sessions():
    return monitor.list_sessions(limit=100)


def context(session_id):
    return context_digest(monitor.read_session(_text(session_id,'session_id'),limit=20))
