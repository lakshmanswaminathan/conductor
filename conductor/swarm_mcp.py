"""Authenticated loopback JSON-RPC MCP endpoint for local Pi peers."""
from __future__ import annotations
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .swarm import SwarmBroker
from .codex_inbox import TOOLS as CODEX_TOOLS, call as codex_call

PROTOCOL = '2025-03-26'

def _tool(name, description, properties, required=()):
    return {'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}

S = {'type':'string'}
I = {'type':'integer'}
TOOL_DEFINITIONS = [
    _tool('swarm_agents','List live and recorded peer sessions.',{}),
    _tool('swarm_context','Read bounded public peer context, never private reasoning.',{'session_id':S},['session_id']),
    _tool('swarm_send','Send a durable peer message. Delivered does not mean completed; no automatic model wake.',{'to':S,'message':{'type':'string','maxLength':4000},'request_id':S,'parent_id':I},['to','message','request_id']),
    _tool('swarm_inbox','Read caller pending mailbox; acknowledge only after durable context acceptance.',{'after':I,'limit':{'type':'integer','minimum':1,'maximum':50}}),
    _tool('swarm_ack','Acknowledge context delivery, not task completion.',{'message_id':I},['message_id']),
    _tool('swarm_task','Create, atomically claim, update owned tasks, or list. Dependencies must complete before work.',{'action':{'type':'string','enum':['create','claim','update','list']},'task_id':S,'title':S,'description':S,'dependencies':{'type':'array','items':S},'status':{'type':'string','enum':['pending','in_progress','blocked','completed','failed']},'result':S,'request_id':S,'owner':S},['action']),
    _tool('swarm_publish','Publish a concise sanitized finding with evidence source references.',{'summary':{'type':'string','maxLength':4000},'sources':{'type':'array','items':S},'request_id':S},['summary','sources','request_id']),
    _tool('swarm_events','Read bounded coordination event log.',{'after':I,'limit':{'type':'integer','minimum':1,'maximum':50}}),
] + CODEX_TOOLS

class SwarmService:
    def __init__(self, root, monitor=None, enabled=lambda: True):
        self.broker = SwarmBroker(root,monitor)
        self.monitor, self.enabled = self.broker.monitor, enabled
        self.token, self.bootstrap_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.registrations = {}
        self.lifecycle = {}
        self.lock = threading.RLock()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *args):
                pass

            def reply(self, status, payload=None):
                raw = json.dumps(payload).encode() if payload is not None else b''
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                expected = f'127.0.0.1:{service.server.server_port}'
                if self.headers.get('Host') != expected or self.headers.get('Origin') not in (None,'http://'+expected):
                    return self.reply(403,{'error':'Invalid host or origin'})
                self.reply(405,{'error':'POST required'})

            def do_POST(self):
                if self.headers.get('Host') != f'127.0.0.1:{service.server.server_port}':
                    return self.reply(403,{'error':'Invalid host'})
                origin = self.headers.get('Origin')
                if origin and origin != f'http://127.0.0.1:{service.server.server_port}':
                    return self.reply(403,{'error':'Invalid origin'})
                if not service.enabled():
                    return self.reply(403,{'error':'Swarm disabled'})
                try:
                    size = int(self.headers.get('Content-Length','0'))
                    if size < 1 or size > 65536 or self.headers.get('Transfer-Encoding'):
                        return self.reply(413,{'error':'Invalid body size'})
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body,dict):
                        raise ValueError('Expected object')
                except (ValueError, UnicodeError):
                    return self.reply(400,{'error':'Invalid JSON body'})
                auth = self.headers.get('Authorization','')
                bearer = auth[7:] if auth.startswith('Bearer ') else ''
                if self.path == '/register':
                    if not secrets.compare_digest(bearer,service.bootstrap_token):
                        return self.reply(401,{'error':'Unauthorized'})
                    sid, instance = body.get('session_id'), body.get('instance_id')
                    if not isinstance(sid,str) or not isinstance(instance,str) or sid == 'conductor' or not service.live(sid,instance):
                        return self.reply(403,{'error':'Exact live session instance required'})
                    with service.lock:
                        for old,record in list(service.registrations.items()):
                            if record['session_id'] == sid:
                                del service.registrations[old]
                                service.lifecycle.pop(old,None)
                        token = secrets.token_urlsafe(32)
                        service.registrations[token] = {'session_id':sid,'instance_id':instance,'last_seen':time.time()}
                    return self.reply(200,{'token':token,'session_id':sid,'instance_id':instance})
                if self.path != '/mcp':
                    return self.reply(404,{'error':'Unknown path'})
                actor = service.actor(bearer)
                if not actor:
                    return self.reply(401,{'error':'Unauthorized or expired live instance'})
                ident = body.get('id')
                if body.get('jsonrpc') != '2.0' or not isinstance(body.get('method'),str):
                    return self.reply(200,{'jsonrpc':'2.0','id':ident,'error':{'code':-32600,'message':'Invalid Request'}})
                method, params = body['method'], body.get('params',{})
                if not isinstance(params,dict):
                    return self.reply(200,{'jsonrpc':'2.0','id':ident,'error':{'code':-32602,'message':'Invalid params'}})
                if 'id' not in body:
                    if method == 'notifications/initialized':
                        with service.lock:
                            if service.lifecycle.get(bearer) == 'initializing':
                                service.lifecycle[bearer] = 'ready'
                    return self.reply(202)
                response = {'jsonrpc':'2.0','id':ident}
                if method == 'initialize':
                    with service.lock:
                        service.lifecycle[bearer] = 'initializing'
                    response['result'] = {'protocolVersion':PROTOCOL,'capabilities':{'tools':{}},'serverInfo':{'name':'conductor-swarm','version':'1.0.0'}}
                elif method == 'ping':
                    response['result'] = {}
                elif method.startswith('tools/') and service.lifecycle.get(bearer) != 'ready':
                    response['error'] = {'code':-32000,'message':'Initialize and send notifications/initialized before using tools'}
                elif method == 'tools/list':
                    response['result'] = {'tools':TOOL_DEFINITIONS}
                elif method == 'tools/call':
                    try:
                        name, arguments = params.get('name'), params.get('arguments',{})
                        result = (codex_call(service.broker.root, actor, name, arguments)
                                  if name in {'codex_send','codex_status'} else service.broker.call(actor,name,arguments))
                        response['result'] = {'content':[{'type':'text','text':json.dumps(result)}],'structuredContent':result,'isError':False}
                    except (ValueError,TypeError,KeyError) as exc:
                        response['result'] = {'content':[{'type':'text','text':str(exc)}],'isError':True}
                else:
                    response['error'] = {'code':-32601,'message':'Method not found'}
                return self.reply(200,response)

        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/mcp'
        self.descriptor = self.broker.root / 'endpoint.json'
        temporary = self.descriptor.with_name('endpoint.'+secrets.token_hex(8)+'.tmp')
        fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump({'version':1,'url':self.url,'bootstrap_token':self.bootstrap_token},f)
        os.replace(temporary,self.descriptor)
        self.thread = threading.Thread(target=self.server.serve_forever,name='conductor-swarm',daemon=True)
        self.thread.start()
        self.relay_stop = threading.Event()
        self.relay_error = None
        def relay():
            from .pi_router import relay_results
            while not self.relay_stop.wait(3):
                if not self.enabled():
                    continue
                try:
                    relay_results(self.broker.root)
                    self.relay_error = None
                except Exception:
                    self.relay_error = 'Pi result relay could not reach Codex; delivery records are retained.'
        self.relay_thread = threading.Thread(target=relay,name='conductor-pi-results',daemon=True)
        self.relay_thread.start()

    def live(self, sid, instance):
        return bool(self.monitor and self.monitor.bridges().get(sid,{}).get('instance_id') == instance)

    def actor(self, token):
        if secrets.compare_digest(token,self.token):
            return 'conductor'
        with self.lock:
            record = self.registrations.get(token)
            if not record or not self.live(record['session_id'],record['instance_id']):
                return None
            record['last_seen'] = time.time()
            return record['session_id']

    def status(self):
        with self.lock:
            return {'enabled':bool(self.enabled()),'url':self.url,'connected_agents':[dict(r) for r in self.registrations.values() if time.time()-r['last_seen'] < 15 and self.live(r['session_id'],r['instance_id'])]}

    def close(self):
        self.relay_stop.set()
        self.relay_thread.join(timeout=1)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        try:
            if json.loads(self.descriptor.read_text()).get('bootstrap_token') == self.bootstrap_token:
                self.descriptor.unlink()
        except (OSError,ValueError):
            pass
        self.broker.close()


def start_swarm(root, monitor=None, enabled=lambda: True):
    return SwarmService(root,monitor,enabled)
