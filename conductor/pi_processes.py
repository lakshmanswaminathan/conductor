"""Read-only process evidence; never inspect command arguments or environments.

A process existing (or having children) is not evidence that its model is working.
Identities use PID plus ps lstart so callers can reject recycled PIDs.
"""
from __future__ import annotations

import os
import subprocess


def normalize_tty(value):
    if not isinstance(value, str):
        return None
    value = value.strip().removeprefix('/dev/')
    return value if value not in {'', '?', '??', '-'} else None


def parse_snapshot(output, limit=4096):
    """Parse LC_ALL=C ps output with fixed five-word lstart, bounded records."""
    records = []
    seen = set()
    for line in output.splitlines():
        parts = line.split(None, 9)
        if len(parts) != 10:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pid <= 0 or ppid < 0 or pid in seen:
            continue
        # comm can contain spaces; retain only its executable basename.
        executable = os.path.basename(parts[9].strip())[:256]
        if not executable:
            continue
        seen.add(pid)
        records.append({'pid':pid, 'ppid':ppid, 'tty':normalize_tty(parts[2]),
                        'stat':parts[3][:16], 'started_at':' '.join(parts[4:9]),
                        'executable':executable})
        if len(records) >= max(1,min(int(limit),16384)):
            break
    return records


def snapshot(limit=4096):
    """Return current-user process records; unavailable ps raises to the caller.

    Scope is the current OS user, so peer processes from other users are excluded.
    ps is supported on macOS and Linux; lstart is kept verbatim in the C locale.
    """
    result = subprocess.run(['ps','-U',str(os.getuid()),'-o','pid=,ppid=,tty=,stat=,lstart=,comm='],
                            capture_output=True,text=True,timeout=3,check=True,
                            env={**os.environ,'LC_ALL':'C'})
    return parse_snapshot(result.stdout,limit)


def identity(record):
    return {key:record.get(key) for key in ('pid','started_at')}


def _index(records):
    return {r['pid']:r for r in records if type(r.get('pid')) is int}


def _current(index, pid, started_at):
    record = index.get(pid)
    return record if record and (started_at is None or record.get('started_at') == started_at) else None


def ancestors(records, pid, limit=64):
    """Nearest parent first; cycles and missing parents terminate traversal."""
    index = _index(records)
    result, seen = [], {pid}
    current = index.get(pid)
    while current and len(result) < max(0,min(int(limit),256)):
        parent = index.get(current.get('ppid'))
        if not parent or parent['pid'] in seen:
            break
        seen.add(parent['pid'])
        result.append(dict(parent))
        current = parent
    return result


def descendants(records, pid, started_at=None, limit=100):
    """All bounded descendants, including shells and children without a TTY."""
    index = _index(records)
    if not _current(index,pid,started_at):
        return []
    children = {}
    for record in index.values():
        children.setdefault(record.get('ppid'),[]).append(record)
    result, queue, seen = [], [pid], {pid}
    maximum = max(0,min(int(limit),1000))
    for parent in queue:
        for child in sorted(children.get(parent,[]),key=lambda r:r['pid']):
            if child['pid'] in seen:
                continue
            if len(result) >= maximum:
                return result
            seen.add(child['pid'])
            result.append(dict(child))
            queue.append(child['pid'])
    return result


def terminal_binding(records, pid, terminals, started_at=None):
    """Bind by own TTY, or nearest ancestor TTY if detached; never title/cwd.

    Duplicate terminal TTYs remain ambiguous. Ancestor matching is explicitly
    identified as lineage evidence rather than an exact process TTY match.
    """
    index = _index(records)
    record = _current(index,pid,started_at)
    if not record:
        return {'status':'stale' if started_at is not None else 'unbound'}
    source = next((r for r in [record]+ancestors(records,pid) if normalize_tty(r.get('tty'))),None)
    if source is None:
        return {'status':'unbound'}
    tty = normalize_tty(source['tty'])
    matches = [dict(t) for t in terminals if normalize_tty(t.get('tty')) == tty]
    evidence = {'tty':tty,'via':'process' if source['pid'] == pid else 'ancestor','source_pid':source['pid']}
    if len(matches) == 1:
        return {'status':'confirmed','terminal':matches[0],**evidence}
    if matches:
        return {'status':'ambiguous','matches':matches[:20],**evidence}
    return {'status':'unbound',**evidence}


def unobserved_pi(records, observed):
    """Pi processes lacking an identity-confirmed observer.

    Observers without started_at cannot establish PID identity and are returned
    with observer_identity_unverified, allowing callers to explain uncertainty.
    """
    if isinstance(observed,dict):
        observed = observed.values()
    observers = {}
    for item in observed:
        if isinstance(item,dict):
            observers.setdefault(item.get('pid'),[]).append(item)
    result = []
    for record in records:
        if record.get('executable') != 'pi' or 'Z' in record.get('stat',''):
            continue
        candidates = observers.get(record['pid'],[])
        if any(o.get('started_at') and o['started_at'] == record.get('started_at') for o in candidates):
            continue
        reason = 'observer_identity_unverified' if any(not o.get('started_at') for o in candidates) else 'observer_identity_stale' if candidates else 'observer_missing'
        result.append(dict(record,reason=reason))
    return result
