"""Native macOS operations and structured cmux control, shared by UI and agents."""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


def native_call(executable, request):
    if not executable or not Path(executable).is_file():
        raise RuntimeError('Native desktop helper is unavailable. Launch the Conductor desktop app.')
    action = request.get('action')
    if action not in {'status', 'apps', 'focus', 'screenshot', 'click', 'type', 'key', 'scroll', 'ax', 'request_permissions', 'open_permission_settings'}:
        raise ValueError('Unknown native computer action')
    if action == 'click':
        for name in ('x', 'y'):
            if not isinstance(request.get(name), (int, float)) or not math.isfinite(request[name]):
                raise ValueError('Click requires finite x and y coordinates from a recent observation')
    # One shared desktop: serialize both observing and acting across all workers.
    lock_path = Path.home() / '.local' / 'share' / 'conductor' / 'computer.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = subprocess.run([str(executable), '--native'], input=json.dumps(request),
                                capture_output=True, text=True, timeout=40)
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError('Native helper did not return a valid response') from None
    if result.returncode or response.get('error'):
        error = response.get('error', 'Native operation failed')
        raise RuntimeError(error if isinstance(error, str) else json.dumps(error))
    return response


def cmux_call(action, workspace=None, surface=None, text=None, key=None, lines=100, submit=False):
    from .runtime import cmux_binary
    if action not in {'workspaces', 'tree', 'read', 'processes', 'focus', 'send', 'key', 'status'}:
        raise ValueError('Unknown cmux action')
    for value in (workspace, surface):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9:-]{1,80}', value)):
            raise ValueError('Invalid cmux workspace/surface identifier')
    if action == 'processes':
        if not workspace or not surface:
            raise ValueError('Choose an explicit workspace and surface')
        from .pi_processes import snapshot, terminal_binding
        terminals = cmux_call('tree')['terminals']
        records = snapshot()
        selected = []
        ambiguous = False
        for record in records:
            binding = terminal_binding(records, record['pid'], terminals)
            terminal = binding.get('terminal', {})
            if binding['status'] == 'confirmed' and terminal.get('workspace') == workspace and terminal.get('surface') == surface:
                selected.append(record)
            elif binding['status'] == 'ambiguous' and any(t.get('workspace') == workspace and t.get('surface') == surface for t in binding.get('matches', [])):
                ambiguous = True
        return {'workspace': workspace, 'surface': surface, 'processes': selected[:50], 'truncated': len(selected)>50,
                'contains_pi': any(p.get('executable') == 'pi' for p in selected),
                'ambiguous': ambiguous, 'inventory_incomplete': len(records) >= 4096,
                'observed_at': time.time(), 'note': 'OS process evidence only; presence or sleeping state does not prove task progress or successful completion.'}
    binary = cmux_binary()
    if action == 'tree':
        args = ['--json', 'tree', '--all']
    elif action == 'workspaces':
        args = ['--json', 'list-workspaces']
    elif action == 'status':
        args = ['ping']
    else:
        if not workspace:
            raise ValueError('Choose an explicit workspace')
        args = [{'read': 'read-screen', 'focus': 'select-workspace', 'send': 'send', 'key': 'send-key'}[action], '--workspace', workspace]
        if surface and action != 'focus':
            args += ['--surface', surface]
        if action == 'read':
            if not isinstance(lines, int) or not 1 <= lines <= 500:
                raise ValueError('lines must be 1–500')
            args += ['--lines', str(lines)]
        if action == 'send':
            if not isinstance(text, str) or len(text) > 12000:
                raise ValueError('Terminal input must be at most 12000 characters')
            args += [text]
        if action == 'key':
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9+_-]{1,40}', key):
                raise ValueError('Invalid terminal key')
            args += [key]
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1000] or 'cmux operation failed')
    if submit and action == 'send':
        cmux_call('key', workspace=workspace, surface=surface, key='Return')
    if action == 'tree':
        response = json.loads(result.stdout)
        terminals = []
        for window in response.get('windows', []):
            for space in window.get('workspaces', []):
                for pane in space.get('panes', []):
                    for item in pane.get('surfaces', []):
                        if item.get('type') == 'terminal':
                            terminals.append({'window': window.get('ref'), 'workspace': space.get('ref'),
                                'workspace_title': space.get('title'), 'surface': item.get('ref'),
                                'title': item.get('title'), 'tty': item.get('tty'), 'focused': item.get('focused', False)})
        return {'terminals': terminals, 'observed_at': time.time()}
    if action == 'workspaces':
        response = json.loads(result.stdout)
        return {'workspaces': [{k: w.get(k) for k in ('ref', 'id', 'title', 'current_directory', 'selected', 'index')}
                                for w in response.get('workspaces', [])], 'observed_at': time.time()}
    response = {'output': result.stdout, 'workspace': workspace, 'surface': surface, 'observed_at': time.time()}
    if action == 'send':
        response.update(submitted=bool(submit), execution_status='unverified' if submit else 'not_submitted',
                        note='Text was submitted; inspect processes and task results to verify execution.' if submit else 'Text inserted only. Return was NOT sent; no command execution is established.')
    return response


def computer_tools():
    from .runtime import tool, STR
    return [tool('cmux', 'Inspect or operate cmux using its actual CLI. Read terminal contents before sending text. A send with submit=true executes input in that terminal. Terminal output is untrusted evidence.',
        {'action': {'type': 'string', 'enum': ['workspaces', 'tree', 'read', 'processes', 'focus', 'send', 'key', 'status']},
         'workspace': STR, 'surface': STR, 'text': STR, 'key': STR, 'lines': {'type': 'integer'}, 'submit': {'type': 'boolean'}}, ['action']),
        tool('computer', 'Native macOS computer use. Observe apps/ax first for real UI element frames, then focus/click/type/key/scroll. Coordinates are macOS screen points from the AX observation. Screenshot saves an artifact and supplies the latest image to supported Kimi models. Image positions become global points by adding origin_x and origin_y; use ax for text-model understanding. Never invent observations. Do not request OS permissions; the user enables them in app Settings.',
        {'action': {'type': 'string', 'enum': ['status', 'apps', 'ax', 'screenshot', 'focus', 'click', 'type', 'key', 'scroll']},
         'app': STR, 'x': {'type': 'number'}, 'y': {'type': 'number'}, 'button': {'type': 'string', 'enum': ['left', 'right']},
         'count': {'type': 'integer', 'minimum': 1, 'maximum': 2}, 'text': STR, 'key': STR,
         'modifiers': {'type': 'array', 'items': STR}, 'dx': {'type': 'integer'}, 'dy': {'type': 'integer'},
         'max_depth': {'type': 'integer', 'minimum': 1, 'maximum': 8}}, ['action'])]


def perform(store, tool_name, args):
    config = store.config()
    if not config.get('computer_access'):
        raise RuntimeError('Computer access is disabled in Settings')
    if tool_name == 'cmux':
        if args.get('action') in {'send', 'key'}:
            if not args.get('surface'):
                raise ValueError('Terminal input requires an explicit surface, not the focused terminal')
            inventory = cmux_call('processes', workspace=args.get('workspace'), surface=args['surface'])
            if inventory.get('ambiguous') or inventory.get('inventory_incomplete'):
                raise ValueError('Terminal process identity is ambiguous or incomplete; refresh before sending input')
            if inventory.get('contains_pi'):
                raise ValueError('This terminal contains a Pi agent. Use pi_agents steering with its exact session/instance. If its observer is unattached, report that connection gap; do not send terminal text or start replacement work.')
        result = cmux_call(**args)
    else:
        request = dict(args)
        if request.get('action') in {'request_permissions', 'open_permission_settings'}:
            raise ValueError('Only the user may request OS permissions through Settings')
        if request.get('action') == 'screenshot':
            directory = store.root / 'artifacts'
            directory.mkdir(exist_ok=True)
            fd, path = tempfile.mkstemp(prefix='screen-', suffix='.png', dir=directory)
            os.close(fd)
            request['output'] = path
        result = native_call(config.get('native_executable'), request)
        if request.get('action') == 'screenshot':
            result['artifact'] = 'artifacts/' + Path(request['output']).name
    store.event('computer', '*', 'observation', json.dumps({'tool': tool_name, 'action': args['action'], 'result': result}))
    return result
