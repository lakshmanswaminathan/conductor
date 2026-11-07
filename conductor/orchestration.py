"""Durable assignment contracts and coordinator acceptance, separate from reports."""
import json
from pathlib import Path
import time


def begin_scope(store):
    users = store.user_context()
    scope = users[-1]['id'] if users else 0
    store.configure(orchestration_scope=scope)
    return scope


def assignment(store, ident):
    return store.config().get('assignment:' + ident, {})


def managed_agents(store):
    config = store.config()
    scope = config.get('orchestration_scope')
    return [a for a in store.agents() if a['parent'] and
            (scope is None or config.get('assignment:' + a['id'], {}).get('scope') == scope) and
            not config.get('assignment:' + a['id'], {}).get('superseded_by')]


def contract(store, args, kind='local'):
    value = {'kind': kind, 'scope': store.config().get('orchestration_scope')}
    required = store.config().get('ui') or 'acceptance' in args or kind == 'pi'
    for field in ('ownership', 'acceptance'):
        items = args.get(field, [])
        if not isinstance(items, list) or len(items) > 12 or any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in items):
            raise ValueError(field + ' must contain up to 12 short nonempty strings')
        if required and not items:
            raise ValueError('Delegation requires explicit ownership and acceptance checks')
        value[field] = items
    value['review_required'] = bool(value['acceptance'])
    verification = args.get('verification')
    if verification is not None and (not isinstance(verification, str) or not verification.strip() or len(verification) > 4000):
        raise ValueError('verification must be a shell command of 1–4000 characters')
    if verification and not store.config().get('allow_exec'):
        raise ValueError('Execution is disabled; command verification is unavailable')
    value['verification'] = verification
    return value


def review(loop, args):
    store = loop.store
    agent = store.agent(args['agent'])
    if not agent or agent not in managed_agents(store) or agent['state'] != 'review':
        raise ValueError('Only a submitted task in the current scope can be reviewed')
    spec = assignment(store, agent['id'])
    evidence, summary = args.get('evidence'), args.get('summary')
    if type(args.get('accepted')) is not bool or not isinstance(summary, str) or not 1 <= len(summary) <= 2000:
        raise ValueError('Provide accepted and a review summary of 1–2000 characters')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20 or any(not isinstance(x, str) or not x.strip() or len(x) > 1000 for x in evidence):
        raise ValueError('Review needs 1–20 concrete evidence references')
    accepted = args['accepted']
    check = None
    if accepted and spec.get('verification'):
        original_cwd = loop.cwd
        version = len(store.user_context())
        try:
            if agent['cwd']:
                loop.cwd = Path(agent['cwd'])
            check = loop.command({'command': spec['verification'], 'timeout': 120})
        finally:
            loop.cwd = original_cwd
        accepted = check.get('exit_code') == 0 and not check.get('error') and not check.get('stopped')
        if store.config().get('cancel_requested') or len(store.user_context()) != version:
            accepted = False
            check['acceptance_blocked'] = 'User context changed or run cancelled during verification'
    receipt = {'accepted': accepted, 'summary': summary, 'evidence': evidence, 'verification': check,
               'reviewed_at': time.time(), 'reported_result': agent['result']}
    store.event('coordinator', agent['id'], 'acceptance', json.dumps(receipt))
    store.update_agent(agent['id'], state='done' if accepted else 'blocked', result=json.dumps(receipt))
    loop.scheduler.tick()
    return receipt
