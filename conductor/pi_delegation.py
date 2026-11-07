"""Durable, instance-bound Pi delegation; peer completion requires local review."""
import hashlib
import json
import re
import time

from .pi_monitor import monitor
from .orchestration import managed_agents
from .pi_sessions import _clean
from .swarm import SwarmBroker


def _strings(value, name):
    if not isinstance(value, list) or not value or len(value) > 20 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 300 for v in value):
        raise ValueError(name + ' must contain 1–20 nonempty strings of at most 300 characters')
    return list(dict.fromkeys(value))


def _save(store, ident, assignment):
    store.configure(**{'assignment:' + ident: assignment})


def _block(store, ident, reason):
    store.update_agent(ident, state='blocked', result=json.dumps({
        'error': reason, 'recovery': 'Inspect the original Pi session and delivery receipt. '
        'No automatic replay or replacement instance was used.'}), heartbeat=time.time())


def _live(assignment):
    bridge = monitor.bridges().get(assignment['session_id'])
    return bool(bridge and bridge.get('instance_id') == assignment['instance_id'])


def _receipt(store, ident):
    assignment = store.config()['assignment:' + ident]
    agent = store.agent(ident)
    return {'id': ident, 'state': agent['state'], 'session_id': assignment['session_id'],
            'instance_id': assignment['instance_id'], 'swarm_task_id': assignment['swarm_task_id'],
            'request_id': assignment['request_id'], 'delivery': assignment.get('delivery'),
            'review_required': True, 'result': agent.get('result')}


def delegate(store, args):
    """Create/reconcile one assignment; identical requests never repeat uncertain delivery."""
    config = store.config()
    if not config.get('computer_access') or not config.get('swarm_root') or config.get('cancel_requested'):
        raise ValueError('Pi delegation is disabled, unavailable, or cancelled')
    ident, task = args.get('id'), args.get('task')
    if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9_-]{1,48}', ident):
        raise ValueError('Invalid task ID')
    if not isinstance(task, str) or not task.strip() or len(task) > 4000:
        raise ValueError('Task must contain 1–4,000 characters')
    spec = {'kind': 'pi', 'task': task, 'ownership': _strings(args.get('ownership'), 'ownership'),
            'acceptance': _strings(args.get('acceptance'), 'acceptance'),
            'scope': config.get('orchestration_scope'), 'review_required': True}
    for key in ('session_id', 'instance_id'):
        if not isinstance(args.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args[key]):
            raise ValueError('Invalid ' + key)
        spec[key] = args[key]
    dependencies = args.get('dependencies', [])
    if not isinstance(dependencies, list) or len(dependencies) > 50 or any(
            not isinstance(d, str) or d in {ident, 'coordinator'} or not store.agent(d) for d in dependencies):
        raise ValueError('Dependencies must name existing local worker tasks')
    spec['dependencies'] = list(dict.fromkeys(dependencies))
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    prior = config.get('assignment:' + ident)
    if prior:
        if prior.get('fingerprint') != fingerprint:
            raise ValueError('Task ID already belongs to a different assignment')
    else:
        if store.agent(ident):
            raise ValueError('Task ID already exists')
        if len(managed_agents(store)) >= config.get('max_tasks', 100):
            raise ValueError('Run task ceiling reached')
        if not _live(spec):
            raise ValueError('Exact live Pi session and instance required')
        chat = hashlib.sha256(str(store.root.resolve()).encode()).hexdigest()[:20]
        prior = spec | {'fingerprint': fingerprint, 'swarm_task_id': 'pi-' + chat + '-' + ident,
                        'request_id': 'pi-' + chat + '-' + ident, 'phase': 'prepared'}
        # Write intent before any broker or harness mutation; retry can repair a
        # crash between intent persistence and local agent creation.
        _save(store, ident, prior)
    if not store.agent(ident):
        store.add_agent(ident, 'coordinator', task, spec['dependencies'])
    tick(store)
    return _receipt(store, ident)


def _advance(store, agent, assignment, broker):
    ident = agent['id']
    dependencies = [store.agent(d) for d in agent['deps']]
    if any(not d or d['state'] in {'failed', 'blocked', 'interrupted'} for d in dependencies):
        _block(store, ident, 'A local dependency failed or was interrupted')
        return
    if any(d['state'] != 'done' for d in dependencies):
        return
    if not _live(assignment):
        _block(store, ident, 'The assigned Pi observer is missing or its instance changed')
        return
    phase = assignment.get('phase')
    if phase == 'sending':
        _block(store, ident, 'Delivery outcome is unknown after an interrupted attempt')
        return
    if phase == 'prepared':
        broker.call('conductor', 'swarm_task', {
            'action': 'create', 'task_id': assignment['swarm_task_id'],
            'title': agent['task'][:300], 'description': agent['task'],
            'owner': assignment['session_id'], 'dependencies': [],
            'request_id': assignment['request_id'] + '-create'})
        message = ('Conductor delegated task ' + assignment['swarm_task_id'] + '\n' +
                   json.dumps({'task': agent['task'], 'ownership': assignment['ownership'],
                               'acceptance': assignment['acceptance']}, ensure_ascii=False) +
                   '\nUse swarm_task to claim this exact task, then update it to completed with '
                   'a result citing verification evidence, or blocked/failed with the reason. '
                   'Use stable request_id values for mutations. Stay within the assigned ownership. '
                   'Reported completion requires Conductor review; do not treat it as final acceptance.')
        if len(message) > 12000:
            _block(store, ident, 'Assignment exceeds the Pi delivery size limit')
            return
        assignment = assignment | {'phase': 'sending'}
        _save(store, ident, assignment)
        # A lost response cannot authorize another delivery, including after restart.
        try:
            delivery = monitor.command(assignment['session_id'], assignment['instance_id'],
                                       assignment['request_id'], 'follow_up', message)
        except Exception:
            delivery = {'status': 'unknown'}
        assignment = assignment | {'delivery': _clean(delivery),
                                    'phase': 'accepted' if delivery.get('status') == 'accepted' else 'uncertain'}
        _save(store, ident, assignment)
        if assignment['phase'] != 'accepted':
            _block(store, ident, 'Pi delivery was ' + str(delivery.get('status', 'unknown')))
            return
        store.update_agent(ident, state='running', heartbeat=time.time(), attempt=agent['attempt'] + 1)
    elif phase != 'accepted':
        _block(store, ident, 'Pi delivery requires explicit reconciliation')
        return
    # Exact lookup avoids losing assignments behind the shared board's 200-row UI cap.
    with broker.lock:
        row = broker.db.execute('SELECT owner,status,result FROM tasks WHERE task_id=?',
                                (assignment['swarm_task_id'],)).fetchone()
    if not row or row['owner'] != assignment['session_id']:
        _block(store, ident, 'Assigned swarm task is missing or its ownership changed')
        return
    status = row['status']
    result = json.dumps(_clean({'session_id': assignment['session_id'],
                               'instance_id': assignment['instance_id'],
                               'swarm_task_id': assignment['swarm_task_id'],
                               'reported_status': status, 'reported_result': row['result']}))
    store.update_agent(ident, state='review' if status == 'completed' else
                       'blocked' if status in {'failed', 'blocked'} else 'running',
                       result=result, heartbeat=time.time())


def tick(store):
    """Reconcile durable Pi assignments without terminal input or automatic replay."""
    config = store.config()
    if not config.get('computer_access') or not config.get('swarm_root') or config.get('cancel_requested'):
        return
    assignments = [(a, config.get('assignment:' + a['id'])) for a in managed_agents(store)
                   if a['parent'] and a['state'] in {'pending', 'running'}]
    assignments = [(a, s) for a, s in assignments if s and s.get('kind') == 'pi']
    if not assignments:
        return
    broker = SwarmBroker(config['swarm_root'], monitor)
    try:
        for agent, assignment in assignments:
            if not store.config().get('computer_access') or store.config().get('cancel_requested'):
                break
            try:
                _advance(store, agent, assignment, broker)
            except (OSError, ValueError, RuntimeError) as exc:
                _block(store, agent['id'], 'Pi reconciliation failed: ' + str(_clean(str(exc)))[:500])
    finally:
        broker.close()
