"""Wait on durable coordination changes without spending model calls on polling."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .orchestration import managed_agents


def progress(store):
    config = store.config()
    agents = managed_agents(store)
    sessions = sorted({config.get('assignment:' + a['id'], {}).get('session_id')
                       for a in agents} - {None})
    events = store.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE target IN ('coordinator','*') AND kind IN ('instruction','agent_feedback','feedback_resolution','message','result','acceptance')").fetchone()[0]
    facts = {'agents': [(a['id'], a['state'], a['result']) for a in agents],
             'events': events, 'findings': store.claims(), 'swarm': None}
    swarm = Path(config['swarm_root']).resolve()/'swarm.sqlite3' if config.get('swarm_root') else None
    if swarm and swarm.is_file() and sessions:
        # No transcript scans or observer heartbeat changes: wake on meaningful peer events.
        with closing(sqlite3.connect(swarm.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            slots = ','.join('?' for _ in sessions)
            messages = db.execute(f'SELECT COALESCE(MAX(id),0) FROM messages WHERE "to"=? AND sender IN ({slots})', ['conductor', *sessions]).fetchone()[0]
            findings = db.execute(f'SELECT COALESCE(MAX(id),0) FROM findings WHERE actor IN ({slots})', sessions).fetchone()[0]
            facts['swarm'] = [messages, findings]
    cursor = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
    return {'cursor': cursor, 'agents': agents, 'findings': facts['findings']}


def _wait_arguments(args):
    timeout = args.get('timeout_seconds', 60)
    after = args.get('after_cursor')
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError('timeout_seconds must be an integer from 1 to 60')
    if after is not None and (not isinstance(after, str) or len(after) != 64 or
                              any(c not in '0123456789abcdef' for c in after)):
        raise ValueError('after_cursor must be a cursor returned by wait or status')
    return timeout, after


def wait_for_progress(store, scheduler, args):
    timeout, after = _wait_arguments(args)
    start = time.monotonic()
    scheduler.tick()
    initial = progress(store)
    baseline = after or initial['cursor']
    current = initial
    while True:
        if store.config().get('cancel_requested') or store.agent('coordinator')['state'] != 'running':
            reason = 'cancelled'
            break
        if current['cursor'] != baseline:
            reason = 'progress'
            break
        if after is None and any(a['state'] == 'review' for a in current['agents']):
            reason = 'review_ready'
            break
        if not any(a['state'] in {'pending', 'starting', 'running'} for a in current['agents']):
            reason = 'no_active_workers'
            break
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            reason = 'timeout'
            break
        time.sleep(min(0.5, remaining))
        # Never begin another potentially slow reconciliation after this chunk's deadline.
        if time.monotonic() - start >= timeout:
            reason = 'timeout'
            break
        scheduler.tick()
        current = progress(store)
    return current | {'changed': current['cursor'] != baseline, 'reason': reason,
                      'waited_seconds': round(time.monotonic()-start, 2),
                      'next': 'On timeout, wait again with this cursor; do not re-read unchanged Pi traces or repeat steering.'}


def suspend_for_progress(store, scheduler, args=None, resume=False):
    """Keep timeout handling inside the harness, without paid inference to wait again."""
    args = dict(args or {})
    _wait_arguments(args)  # Reject malformed input before persisting a suspended run.
    review = args.pop('review_after_seconds', None)
    if review is not None and (type(review) is not int or not 1 <= review <= 86400):
        raise ValueError('review_after_seconds must be an integer from 1 to 86400')
    saved = store.config().get('coordinator_wait') if resume else None
    state = saved or {'status': 'waiting', 'started_at': time.time(),
                      'cursor': args.get('after_cursor') or progress(store)['cursor'],
                      'review_at': time.time() + review if review else None}
    if not saved and 'after_cursor' not in args and any(a['state'] == 'review' for a in managed_agents(store)):
        return progress(store) | {'reason': 'review_ready', 'changed': False}
    args['after_cursor'] = state['cursor']
    while True:
        store.configure(coordinator_wait=state)
        remaining = state['review_at'] - time.time() if state.get('review_at') else None
        if remaining is not None and remaining <= 0:
            result = progress(store) | {'reason': 'review_deadline', 'changed': False}
            break
        chunk = min(args.get('timeout_seconds', 60), max(1, int(remaining))) if remaining else args.get('timeout_seconds', 60)
        result = wait_for_progress(store, scheduler, args | {'timeout_seconds': chunk})
        if result['reason'] != 'timeout':
            break
        args['after_cursor'] = state['cursor']
    store.configure(coordinator_wait={**state, 'status': 'resumed', 'reason': result['reason'], 'resumed_at': time.time()})
    return result | {'next': 'Review the new event or deadline; unchanged timeout polling was handled without inference.'}
