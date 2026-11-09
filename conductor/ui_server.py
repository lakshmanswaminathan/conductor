"""Authenticated loopback UI for the native Conductor desktop application."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse
import uuid
from urllib.request import Request, urlopen

from .cli import initialize
from .runtime import run_coordinator
from .computer import cmux_call, native_call
from .provider import OpenRouter, validate_key
from .store import Store
from .models import DEFAULT_MODEL
from .pi_sessions import _clean
from .conversation_labels import conversation_labels, compact, request_text


def _activity_label(name, args):
    actions = {
        'pi_agents': {'list':'Check Pi sessions', 'context':'Read Pi context', 'read':'Read Pi trace', 'steer':'Send instruction to Pi'},
        'cmux': {'tree':'Check terminal sessions', 'read':'Read terminal output', 'processes':'Check terminal processes',
                 'send':'Send terminal input', 'key':'Send terminal key', 'focus':'Focus terminal'},
        'swarm_task': {'list':'Check shared tasks', 'create':'Create shared task', 'claim':'Claim shared task', 'update':'Update shared task'},
    }
    if name in actions and args.get('action') in actions[name]:
        return actions[name][args['action']]
    labels = {'pi_agents':'Inspect Pi agents', 'pi_context':'Read Pi context',
              'pi_control':'Contact Pi agent', 'swarm_agents':'Inspect peers',
              'swarm_context':'Read peer context', 'swarm_send':'Send peer message',
              'swarm_inbox':'Check messages', 'swarm_ack':'Acknowledge message',
              'swarm_task':'Update shared task', 'swarm_publish':'Publish finding',
              'swarm_events':'Read coordination activity', 'finish':'Finish response',
              'exec':'Run command', 'shell':'Run command', 'read_file':'Read file',
              'write_file':'Write file', 'cmux':'Inspect terminal', 'native':'Inspect desktop'}
    label = labels.get(name, name.replace('_',' ').strip().capitalize() or 'Tool activity')
    context = next((args[k] for k in ('action','path','session_id','task_id','to')
                    if isinstance(args.get(k),str) and args[k]),None)
    return _clean(label + (' · ' + context[:100] if context else ''))


def _activity_detail(arguments, output=None, has_output=False):
    detail = {'arguments':arguments}
    if has_output:
        detail['output'] = output
    encoded = json.dumps(_clean(detail),ensure_ascii=False)
    return encoded if len(encoded) <= 6000 else encoded[:5980] + ' … [truncated]'


class App:
    def __init__(self, root=None, native=None, provider_factory=None):
        self.root = Path(root or Path.home() / '.local' / 'share' / 'conductor' / 'runs').resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.native = str(native) if native else None
        self.key = os.environ.get('OPENROUTER_API_KEY', '')
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs, self.stopping = {}, set()
        self.model_catalog = None
        self.model_catalog_at = 0
        self.model_catalog_lock = threading.Lock()
        self.provider_factory = provider_factory or (lambda store, key: OpenRouter(
            store.config()['model'], key=key, max_calls=store.config()['max_calls'], state_path=store.root))
        self.settings_path = self.root.parent / 'ui-settings.json'
        self.settings = {'model': DEFAULT_MODEL, 'concurrency': 3, 'computer_access': True, 'engine': 'conductor'}
        if self.settings_path.exists():
            try:
                self.settings.update(self.validate_settings(json.loads(self.settings_path.read_text())))
            except (ValueError, TypeError):
                pass
        self.settings['engine'] = 'conductor'
        self.screens = tempfile.TemporaryDirectory(prefix='conductor-ui-', dir='/tmp')
        from .swarm_mcp import start_swarm
        self.swarm = start_swarm(self.root.parent / 'swarm',
                                 enabled=lambda: bool(self.settings.get('computer_access')))
        self.mailbox_stop = threading.Event()
        from .conductor_mailbox import recover
        interrupted = recover(self.root.parent)
        for path in self.root.glob('*/state.sqlite'):
            store = Store(path.parent)
            try:
                agent = store.agent('coordinator')
                if agent and agent['state'] in {'running', 'starting'}:
                    store.update_agent('coordinator', state='interrupted', result='Conductor restarted; inspect Pi progress before resuming')
            finally:
                store.close()
        self.mailbox_status = {'pending': 0, 'interrupted': interrupted}
        self.mailbox_thread = threading.Thread(target=self._mailbox_loop, name='conductor-mailbox', daemon=True)
        self.mailbox_thread.start()

    def models(self):
        # The public catalog needs no credential and consumes no inference calls.
        with self.model_catalog_lock:
            if self.model_catalog is not None and time.monotonic() - self.model_catalog_at < 300:
                return {'models': self.model_catalog}
            try:
                request = Request('https://openrouter.ai/api/v1/models', headers={'Accept': 'application/json'})
                with urlopen(request, timeout=15) as response:
                    rows = json.load(response)['data']
                models = {}
                for row in rows:
                    ident = row.get('id')
                    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}', ident):
                        continue
                    if 'text' not in row.get('architecture', {}).get('output_modalities', ['text']):
                        continue
                    models[ident] = {'id': ident, 'name': str(row.get('name') or ident)}
                if not models:
                    raise ValueError('Empty catalog')
                self.model_catalog = sorted(models.values(), key=lambda model: model['name'].casefold())
                self.model_catalog_at = time.monotonic()
                return {'models': self.model_catalog}
            except Exception:
                if self.model_catalog is not None:
                    return {'models': self.model_catalog, 'warning': 'OpenRouter is unavailable. Showing the last loaded catalog.'}
                raise ValueError('Could not load OpenRouter models. Try again.') from None

    def validate_settings(self, data):
        result = {}
        if 'engine' in data:
            if data['engine'] not in ('codex', 'conductor'):
                raise ValueError('Choose Codex or Conductor as the harness')
            result['engine'] = 'conductor'
        if 'model' in data:
            if not isinstance(data['model'], str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}', data['model']):
                raise ValueError('Enter a valid OpenRouter model ID')
            result['model'] = data['model']
        if 'concurrency' in data:
            if type(data['concurrency']) is not int or not 1 <= data['concurrency'] <= 8:
                raise ValueError('Choose between 1 and 8 workers')
            result['concurrency'] = data['concurrency']
        if 'computer_access' in data:
            if type(data['computer_access']) is not bool:
                raise ValueError('computer_access must be true or false')
            result['computer_access'] = data['computer_access']
        return result

    def update_settings(self, data):
        values = self.validate_settings(data)
        key = data.get('api_key')
        if key is not None:
            key = validate_key(key)
        with self.lock:
            if key is not None:
                self.key = key.strip()
            self.settings.update(values)
            temporary = self.settings_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.settings))
            os.chmod(temporary, 0o600)
            temporary.replace(self.settings_path)
            if 'computer_access' in values:
                for path in self.root.glob('*/state.sqlite'):
                    store = Store(path.parent)
                    try:
                        store.configure(computer_access=values['computer_access'])
                    finally:
                        store.close()
        return {'key_configured': bool(self.key), 'settings': self.settings}

    def path(self, ident):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', ident):
            raise ValueError('Invalid chat ID')
        path = self.root / ident
        if not (path / 'state.sqlite').is_file():
            raise FileNotFoundError('Chat not found')
        return path

    def is_running(self, ident):
        thread = self.jobs.get(ident)
        return bool(thread and thread.is_alive())

    def chats(self):
        result = []
        for file in self.root.glob('*/state.sqlite'):
            store = Store(file.parent)
            try:
                config, agent = store.config(), store.agent('coordinator')
                if not agent:
                    continue
                updated = store.db.execute('SELECT MAX(at) FROM events').fetchone()[0] or file.stat().st_mtime
                result.append({'id': file.parent.name, **conversation_labels(store, config, agent),
                    'state': 'running' if self.is_running(file.parent.name) else agent['state'], 'updated_at': updated, 'model': config.get('model')})
            finally:
                store.close()
        return sorted(result, key=lambda x: x['updated_at'], reverse=True)

    def snapshot(self, ident):
        store = Store(self.path(ident))
        try:
            config = store.config()
            messages = []
            for row in store.db.execute("SELECT * FROM events WHERE (source='user' AND kind='instruction') OR (target='user' AND kind='error') ORDER BY id"):
                messages.append({'id': 'event-' + str(row['id']), 'role': 'user' if row['source'] == 'user' else 'assistant',
                    'kind': row['kind'], 'content': row['body'], 'at': row['at']})
            running = self.is_running(ident)
            activities, arguments, pending_finishes = {}, {}, {}
            for row in store.db.execute("SELECT * FROM turns WHERE agent='coordinator' ORDER BY id"):
                message = json.loads(row['message'])
                if message['role'] == 'assistant':
                    if message.get('content'):
                        messages.append({'id': 'turn-' + str(row['id']), 'role': 'assistant', 'kind': 'text', 'content': message['content'], 'at': row['at'],
                                         'agent': 'Codex' if message.get('bridge_request_id') else 'Conductor'})
                    for call in message.get('tool_calls', []):
                        name, call_id = call['function']['name'], call['id']
                        try:
                            args = json.loads(call['function']['arguments'])
                        except (ValueError,TypeError):
                            args = {'raw':call['function'].get('arguments','')}
                        if not isinstance(args,dict):
                            args = {'value':args}
                        arguments[call_id] = args
                        if name == 'finish':
                            pending_finishes[call_id] = args.get('summary','')
                        activity = {'id':'call-' + call_id,'role':'assistant','kind':'tool',
                            'tool_name':name,'tool_call_id':call_id,'label':_activity_label(name,args),
                            'status':'running' if running else 'interrupted','duration_ms':None,
                            'content':_activity_detail(args),'at':row['at']}
                        activities[call_id] = activity
                        messages.append(activity)
                if message['role'] == 'tool':
                    try:
                        output = json.loads(message.get('content',''))
                    except (ValueError,TypeError):
                        output = message.get('content','')
                    call_id = message.get('tool_call_id')
                    failed = message.get('_success') is False or (isinstance(output,dict) and bool(output.get('error') or output.get('isError') or output.get('is_error')
                        or output.get('exit_code') not in (None, 0) or output.get('status') == 'rejected'))
                    activity = activities.get(call_id)
                    if activity:
                        activity['status'] = 'failed' if failed else 'completed'
                        activity['duration_ms'] = max(0,round((row['at']-activity['at'])*1000))
                        activity['content'] = _activity_detail(arguments[call_id],output,True)
                    if not failed and call_id in pending_finishes and isinstance(output,dict) and ('summary' in output or 'artifact' in output):
                        if activity:
                            messages.remove(activity)
                        messages.append({'id':'finish-' + str(row['id']),'role':'assistant','kind':'text',
                            'content':pending_finishes[call_id], 'findings':_clean(output.get('findings',arguments[call_id].get('findings',[]))),
                            'evidence':_clean(output.get('evidence',arguments[call_id].get('evidence',[]))), 'at':row['at']})
            messages.sort(key=lambda m: m['at'])
            agent = store.agent('coordinator')
            return {'id': ident, **conversation_labels(store, config, agent), 'state': agent['state'],
                'running': running, 'stopping': ident in self.stopping, 'messages': messages,
                'agents': [a for a in store.agents() if a['parent']], 'findings': store.claims(),
                'checkpoints': {a['id']: store.checkpoint(a['id']) for a in store.agents()},
                'usage': config.get('usage', {}), 'model': config['model'], 'computer_access': config.get('computer_access', False),
                'engine': config.get('engine', 'conductor'),
                'bridge_pending': config.get('bridge_pending', False),
                'bridge_delivery': config.get('bridge_delivery'),
                'error': agent['result'] if agent['state'] == 'failed' else None}
        finally:
            store.close()

    def message(self, data):
        value = data.get('message')
        if not isinstance(value, str) or not value.strip() or len(value) > 40000:
            raise ValueError('Enter a message of 1–40,000 characters')
        return value.strip()

    def request_id(self, data):
        request = data.get('request_id') or uuid.uuid4().hex
        if not isinstance(request, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request):
            raise ValueError('Invalid request_id')
        return request

    def _record_request(self, store, message, request):
        fingerprint = hashlib.sha256(message.encode()).hexdigest()
        store.db.execute('CREATE TABLE IF NOT EXISTS ui_requests(request_id TEXT PRIMARY KEY,fingerprint TEXT,event_id INTEGER)')
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            previous = store.db.execute('SELECT * FROM ui_requests WHERE request_id=?', (request,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('request_id already belongs to different content')
                return False
            event = None
            if store.config().get('first_request_id') == request:
                event = store.db.execute("SELECT id,body FROM events WHERE source='user' AND kind='instruction' ORDER BY id LIMIT 1").fetchone()
                if event and event['body'] != message:
                    raise ValueError('request_id already belongs to different content')
            event_id = event['id'] if event else store.db.execute(
                'INSERT INTO events(source,target,kind,body,at) VALUES(?,?,?,?,?)',
                ('user', '*', 'instruction', message, time.time())).lastrowid
            store.db.execute('INSERT INTO ui_requests VALUES(?,?,?)', (request, fingerprint, event_id))
        return True

    def create(self, data):
        message, request = self.message(data), self.request_id(data)
        if not self.key:
            raise ValueError('Add your OpenRouter key in Settings to start')
        ident = 'chat-' + hashlib.sha256(request.encode()).hexdigest()[:24]
        with self.lock:
            if not (self.root / ident / 'state.sqlite').exists():
                settings = self.settings | self.validate_settings(data)
                store = initialize(self.root / ident, Path.home(), message, scope='filesystem', allow_exec=False,
                    execution_policy='pi_only', ui=True, context_budget=96000, native_executable=self.native,
                    title=compact(request_text(message), 100), first_request_id=request, **settings)
            else:
                store = Store(self.path(ident))
            try:
                added = self._record_request(store, message, request)
                should_start = added or store.agent('coordinator')['state'] != 'failed'
            finally:
                store.close()
            if should_start:
                self.start(ident)
        return {'id': ident, 'request_id': request, 'queued': False}

    def submit(self, ident, data):
        message, request = self.message(data), self.request_id(data)
        with self.lock:
            if ident in self.stopping:
                raise ValueError('This chat is still stopping; wait for the current model request to return')
            if not self.key:
                raise ValueError('Add your OpenRouter key in Settings')
            store = Store(self.path(ident))
            try:
                added = self._record_request(store, message, request)
                should_start = added or store.agent('coordinator')['state'] != 'failed'
            finally:
                store.close()
            queued = self.is_running(ident)
            if not queued and should_start:
                self.start(ident)
        return {'id': ident, 'request_id': request, 'queued': queued}

    def set_model(self, ident, data):
        model = self.validate_settings({'model': data.get('model')})['model']
        with self.lock:
            if self.is_running(ident) or ident in self.stopping:
                raise ValueError('Stop this chat and wait for it to finish stopping before changing its model')
            store = Store(self.path(ident))
            try:
                store.configure(model=model)
            finally:
                store.close()
        return {'id': ident, 'model': model}

    def start(self, ident):
        with self.lock:
            if self.is_running(ident):
                return
            if not self.key:
                raise ValueError('Add your OpenRouter key in Settings')
            self.stopping.discard(ident)
            key = self.key
            store = Store(self.path(ident))
            try:
                config = store.config()
                if len(store.user_context()) <= config.get('processed_user_version', 0):
                    return
                store.configure(ui=True, allow_exec=False, execution_policy='pi_only',
                    engine='conductor',
                    model=config.get('model', self.settings['model']) if config.get('engine') in {'codex','conductor'} else self.settings['model'],
                    repo=config.get('repo', str(Path.home())), base=config.get('base'), scope=config.get('scope','filesystem'),
                    max_calls=config.get('max_calls',200), max_turns=config.get('max_turns',0),
                    max_tasks=config.get('max_tasks',12), concurrency=config.get('concurrency',self.settings['concurrency']),
                    transport=config.get('transport','cmux'), demo=config.get('demo',False),
                    native_executable=self.native, computer_access=self.settings['computer_access'],
                    swarm_root=str(self.root.parent / 'swarm'), cancel_requested=False,
                    context_budget=max(96000, config.get('context_budget', 24000)))
            finally:
                store.close()
            def work():
                store = Store(self.path(ident))
                try:
                    while ident not in self.stopping:
                        previous_version = store.config().get('processed_user_version', 0)
                        attempted = store.user_context()
                        run_coordinator(store, self.provider_factory(store, key))
                        self._report_outcome(store, ident, attempted, previous_version)
                        with self.lock:
                            if (store.agent('coordinator')['state'] in {'failed','interrupted'} or
                                    len(store.user_context()) <= store.config().get('processed_user_version', 0)):
                                self.jobs.pop(ident, None)
                                self.stopping.discard(ident)
                                break
                except Exception as exc:
                    store.update_agent('coordinator', state='failed', result=str(exc))
                    store.event('harness', 'user', 'error', str(exc))
                    self._report_outcome(store, ident, attempted if 'attempted' in locals() else [], previous_version if 'previous_version' in locals() else 0)
                finally:
                    store.close()
                    with self.lock:
                        if self.jobs.get(ident) is threading.current_thread():
                            self.jobs.pop(ident, None)
                            self.stopping.discard(ident)
            thread = threading.Thread(target=work, name='conductor-' + ident, daemon=True)
            self.jobs[ident] = thread
            thread.start()

    def stop(self, ident):
        with self.lock:
            self.stopping.add(ident)
            store = Store(self.path(ident))
            try:
                store.configure(cancel_requested=True)
                for agent in store.agents():
                    if agent['state'] in {'pending','starting','running'}:
                        store.update_agent(agent['id'], state='interrupted', result='Stopped by user')
                store.event('user', '*', 'stop', 'Stopped by user')
            finally:
                store.close()
            if not self.is_running(ident):
                self.stopping.discard(ident)
        return {'stopping': self.is_running(ident)}

    def _report_outcome(self, store, ident, attempted, previous_version):
        agent = store.agent('coordinator')
        if agent['state'] not in {'done','completed','failed','blocked'}:
            return
        events = store.user_context()
        version = store.config().get('processed_user_version', 0)
        handled = attempted[previous_version:] if agent['state'] == 'failed' else events[previous_version:version]
        if version <= previous_version and agent['state'] != 'failed':
            handled = attempted[previous_version:]
        if not handled:
            return
        try:
            from .conductor_mailbox import report
            report(self.root.parent, ident, agent['state'], agent['result'] or '', handled[-1]['id'], from_event_id=handled[0]['id'])
        except Exception as exc:
            self.mailbox_status = {'error': 'Could not report coordinator result to Codex: ' + str(exc)}

    def drain_mailbox(self):
        from .conductor_mailbox import pending, ack
        messages = pending(self.root.parent)
        self.mailbox_status = {'pending': len(messages)}
        if messages and not self.key:
            self.mailbox_status['error'] = 'Codex requests are waiting for an OpenRouter key in Settings'
            return
        for message in messages:
            try:
                data = {'message': message['message'], 'request_id': 'codex:' + hashlib.sha256(message['request_id'].encode()).hexdigest()}
                result = self.submit(message['chat_id'], data) if message.get('chat_id') else self.create(data)
                ack(self.root.parent, message['request_id'], result['id'])
            except Exception as exc:
                self.mailbox_status['error'] = str(exc)
        self.mailbox_status['pending'] = len(pending(self.root.parent))

    def _mailbox_loop(self):
        while not self.mailbox_stop.wait(1):
            try:
                self.drain_mailbox()
                from .conductor_mailbox import flush
                flush(self.root.parent)
            except ImportError:
                pass
            except Exception as exc:
                self.mailbox_status = {'error': str(exc)}

    def native_status(self):
        try:
            return native_call(self.native, {'action': 'status'}) | {'available': True}
        except Exception as exc:
            return {'available': False, 'accessibility': False, 'screen_recording': False, 'error': str(exc)}

    def bootstrap(self):
        from .desktop_bridge import status
        try:
            cmux_call('status')
            cmux = {'available': True}
        except Exception as exc:
            cmux = {'available': False, 'error': str(exc)}
        return {'mailbox': dict(self.mailbox_status), 'bridge': status(self.root), 'key_configured': bool(self.key), 'settings': self.settings, 'native': self.native_status(), 'cmux': cmux, 'home': str(Path.home())}

    def close(self):
        self.mailbox_stop.set()
        self.mailbox_thread.join(timeout=2)
        threads = list(self.jobs.values())
        for ident in list(self.jobs):
            if self.is_running(ident):
                self.stop(ident)
        deadline = time.monotonic() + 6
        for thread in threads:
            thread.join(timeout=max(0, deadline-time.monotonic()))
        self.screens.cleanup()
        self.swarm.close()


def make_server(app, port=0):
    static = Path(__file__).parent / 'web'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No URLs, credentials or conversation content in access logs.

        def authorized(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                return False
            header = self.headers.get('X-Conductor-Token', '')
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                supplied = cookie['conductor_session'].value if 'conductor_session' in cookie else ''
            except Exception:
                supplied = ''
            return secrets.compare_digest(header, app.token) or secrets.compare_digest(supplied, app.token)

        def send(self, status, body, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if secrets.compare_digest(self.headers.get('X-Conductor-Token', ''), app.token):
                self.send_header('Set-Cookie', f'conductor_session={app.token}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)

        def route(self, post=False):
            if not self.authorized():
                self.send(401, {'error': 'Open this UI through the Conductor desktop app.'})
                return
            expected = f'http://127.0.0.1:{self.server.server_port}'
            if post and self.headers.get('Origin') not in (None, expected):
                self.send(403, {'error': 'Cross-origin requests are not allowed'})
                return
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            data = {}
            if post:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 100000:
                    raise ValueError('Request too large')
                data = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
            if not post and path in {'/', '/app.js', '/swarm.js', '/app.css'}:
                file = static / ('index.html' if path == '/' else path[1:])
                self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if not post and path.startswith('/screen/'):
                name = path.split('/')[-1]
                if not re.fullmatch(r'[a-f0-9]{32}\.png', name):
                    raise ValueError('Invalid image')
                self.send(200, (Path(app.screens.name) / name).read_bytes(), 'image/png')
                return
            if path == '/api/bootstrap' and not post:
                result = app.bootstrap()
            elif path == '/api/models' and not post:
                result = app.models()
            elif path == '/api/settings' and post:
                result = app.update_settings(data)
            elif path == '/api/chats':
                result = app.create(data) if post else {'chats': app.chats(), 'mailbox': dict(app.mailbox_status)}
            elif re.fullmatch(r'/api/chats/[A-Za-z0-9_-]+(?:/(?:messages|stop|model))?', path):
                parts = path.split('/')
                ident = parts[3]
                if len(parts) == 4 and not post:
                    result = app.snapshot(ident)
                elif len(parts) == 5 and post:
                    if parts[4] == 'model':
                        result = app.set_model(ident, data)
                    else:
                        result = app.submit(ident, data) if parts[4] == 'messages' else app.stop(ident)
                else:
                    raise ValueError('Unsupported chat operation')
            elif path == '/api/cmux' and not post:
                result = cmux_call('workspaces')
            elif path == '/api/cmux/tree' and not post:
                result = cmux_call('tree')
            elif path == '/api/cmux/screen' and not post:
                result = cmux_call('read', workspace=query.get('workspace', [None])[0],
                                   surface=query.get('surface', [None])[0] or None, lines=120)
            elif path == '/api/pi/sessions' and not post:
                from .pi_monitor import monitor
                result = monitor.list_sessions()
            elif path == '/api/swarm' and not post:
                result = self.swarm_snapshot()
            elif path == '/api/swarm/action' and post:
                with app.lock:
                    if not app.settings.get('computer_access'):
                        raise ValueError('Computer access is disabled in Settings')
                    result = app.swarm.broker.call('conductor', data.get('name'), data.get('arguments', {}))
            elif re.fullmatch(r'/api/pi/sessions/[A-Za-z0-9_-]+/steer', path) and post:
                from .pi_monitor import monitor
                with app.lock:
                    if not app.settings.get('computer_access'):
                        raise ValueError('Computer access is disabled in Settings')
                    result = monitor.command(path.split('/')[4], data.get('instance_id'), data.get('request_id'),
                                             data.get('mode', 'steer'), data.get('message'))
            elif path.startswith('/api/pi/sessions/') and not post:
                from .pi_monitor import monitor
                result = monitor.read_session(unquote(path.split('/')[-1]), limit=int(query.get('limit', ['50'])[0]),
                                              before=query.get('before', [None])[0])
            elif path == '/api/cmux/focus' and post:
                result = cmux_call('focus', workspace=data.get('workspace'))
                if app.native:
                    native_call(app.native, {'action': 'focus', 'app': 'cmux'})
            elif path == '/api/computer/status' and not post:
                result = app.native_status()
            elif path == '/api/computer/permissions' and post:
                if data.get('pane') not in {'accessibility', 'screen_recording'}:
                    raise ValueError('Choose Accessibility or Screen Recording')
                result = native_call(app.native, {'action': 'open_permission_settings', 'pane': data['pane']})
            elif path == '/api/computer/capture' and post:
                name = uuid.uuid4().hex + '.png'
                result = native_call(app.native, {'action': 'screenshot', 'app': data.get('app', 'cmux'), 'output': str(Path(app.screens.name) / name)})
                result['image_url'] = '/screen/' + name
                # Retain only a small monitor frame cache.
                files = sorted(Path(app.screens.name).glob('*.png'), key=lambda p: p.stat().st_mtime)
                for old in files[:-8]:
                    old.unlink(missing_ok=True)
            else:
                self.send(404, {'error': 'Not found'})
                return
            self.send(200, result)

        def swarm_snapshot(self):
            if not app.settings.get('computer_access'):
                return {'service': {'enabled': False, 'connected_agents': []},
                        'tasks': [], 'messages': [], 'findings': [], 'events': []}
            return app.swarm.broker.snapshot() | {'service': app.swarm.status()}

        def handle_request(self, post=False):
            try:
                self.route(post)
            except FileNotFoundError as exc:
                self.send(404, {'error': str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self.send(400, {'error': str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self.send(500, {'error': str(exc)})

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(True)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def serve(port=0, native=None, root=None):
    os.umask(0o077)
    app = App(root=root, native=native)
    server = make_server(app, port)
    def terminate(signum, frame):
        raise SystemExit(0)
    previous = signal.signal(signal.SIGTERM, terminate)
    # Consumed only by the parent native process, never written to a log or argv.
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'token': app.token}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        app.close()
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
