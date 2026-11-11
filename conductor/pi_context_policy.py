"""Request native compaction only for exact owned, idle Pi attachments."""
import hashlib
import math
import time

from .orchestration import managed_agents
from .pi_monitor import monitor


def maintain_context(store):
    config = store.config()
    target = config.get('pi_compaction_tokens', 200000)
    if (not config.get('computer_access') or config.get('cancel_requested') or
            type(target) is not int or target <= 0):
        return
    records = monitor.records()
    for agent in managed_agents(store):
        assignment = config.get('assignment:' + agent['id'], {})
        if assignment.get('kind') != 'pi' or agent['state'] != 'running' or assignment.get('phase') != 'accepted':
            continue
        session, instance = assignment['session_id'], assignment['instance_id']
        record = records.get(session, {})
        usage = record.get('context_usage') or {}
        tokens = usage.get('tokens')
        compact = record.get('compaction') or {}
        if (record.get('instance_id') != instance or record.get('status') != 'idle' or
                not 0 <= time.time() - record.get('observed_at', 0) < 15 or
                record.get('pending_messages') or record.get('active_tools') or
                type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens < target or
                not record.get('leaf_id') or compact.get('state') in {'queued', 'running'}):
            continue
        key = 'context_compaction:' + session
        previous = store.config().get(key, {})
        if previous.get('instance_id') == instance and (previous.get('leaf_id') == record['leaf_id'] or
                time.time() - previous.get('at', 0) < 300):
            continue
        request = 'compact-' + hashlib.sha256((session + instance + record['leaf_id']).encode()).hexdigest()[:32]
        intent = {'request_id': request, 'instance_id': instance, 'leaf_id': record['leaf_id'],
                  'tokens_before': tokens, 'target_tokens': target, 'at': time.time(), 'delivery': {'status': 'unknown'}}
        store.configure(**{key: intent})  # Crash/unknown outcome never authorizes repeat delivery.
        try:
            delivery = monitor.command(session, instance, request, 'compact')
        except Exception:
            delivery = {'status': 'unknown'}
        store.configure(**{key: intent | {'delivery': delivery}})
