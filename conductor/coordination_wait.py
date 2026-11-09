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
    events = store.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE target IN ('coordinator','*') AND kind IN ('instruction','message','result','acceptance')").fetchone()[0]
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


def wait_for_progress(store, scheduler, args):
    timeout = args.get('timeout_seconds', 60)
    after = args.get('after_cursor')
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError('timeout_seconds must be an integer from 1 to 60')
    if after is not None and (not isinstance(after, str) or len(after) != 64 or
                              any(c not in '0123456789abcdef' for c in after)):
        raise ValueError('after_cursor must be a cursor returned by wait or status')
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
        scheduler.tick()
        current = progress(store)
    return current | {'changed': current['cursor'] != baseline, 'reason': reason,
                      'waited_seconds': round(time.monotonic()-start, 2),
                      'next': 'On timeout, wait again with this cursor; do not re-read unchanged Pi traces or repeat steering.'}
