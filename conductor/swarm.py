"""Durable, provenance-preserving local swarm coordination. No execution tools."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from .pi_sessions import _clean


def _text(value, field, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{field} must be nonempty text, at most {maximum} characters')
    return _clean(value)


class SwarmBroker:
    def __init__(self, root, monitor=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if monitor is None:
            from .pi_monitor import PiMonitor
            monitor = PiMonitor()
        self.monitor = monitor
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'swarm.sqlite3', check_same_thread=False)
        (self.root / 'swarm.sqlite3').chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT,actor TEXT,data TEXT,created_at REAL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,event_id INTEGER,sender TEXT,"to" TEXT,message TEXT,parent_id INTEGER,root_id INTEGER,hops INTEGER,state TEXT,created_at REAL);
        CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,title TEXT,description TEXT,dependencies TEXT,owner TEXT,status TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY,actor TEXT,summary TEXT,sources TEXT,created_at REAL);
        CREATE TABLE IF NOT EXISTS requests(actor TEXT,request_id TEXT,fingerprint TEXT,response TEXT,PRIMARY KEY(actor,request_id));
        ''')

    def close(self):
        with self.lock:
            self.db.close()

    def _event(self, kind, actor, data):
        return self.db.execute('INSERT INTO events(kind,actor,data,created_at) VALUES(?,?,?,?)',
                               (kind, actor, json.dumps(_clean(data)), time.time())).lastrowid

    def call(self, actor, name, arguments):
        _text(actor, 'actor', 128)
        if not isinstance(arguments, dict):
            raise ValueError('arguments must be an object')
        a = dict(arguments)
        allowed = {
            'swarm_agents': set(), 'swarm_context': {'session_id'},
            'swarm_send': {'to','message','request_id','parent_id'},
            'swarm_inbox': {'after','limit'}, 'swarm_ack': {'message_id'},
            'swarm_task': {'action','task_id','title','description','dependencies','status','result','request_id','owner'},
            'swarm_publish': {'summary','sources','request_id'}, 'swarm_events': {'after','limit'},
        }
        if name not in allowed or set(a) - allowed[name]:
            raise ValueError('Unknown tool or argument')
        mutation = name in {'swarm_send','swarm_publish','swarm_ack'} or (name == 'swarm_task' and a.get('action') != 'list')
        request = a.get('request_id')
        if name in {'swarm_send','swarm_publish'} or (name == 'swarm_task' and mutation):
            _text(request, 'request_id', 128)
        if request is not None:
            _text(request, 'request_id', 128)
        fingerprint = hashlib.sha256(json.dumps([name,a], sort_keys=True).encode()).hexdigest()
        with self.lock, self.db:
            if mutation:
                self.db.execute('BEGIN IMMEDIATE')
            if request:
                previous = self.db.execute('SELECT * FROM requests WHERE actor=? AND request_id=?', (actor,request)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise ValueError('request_id reused with different arguments')
                    return json.loads(previous['response'])
            result = self._call(actor,name,a)
            if request:
                self.db.execute('INSERT INTO requests VALUES(?,?,?,?)', (actor,request,fingerprint,json.dumps(result)))
            return result

    def _call(self, actor, name, a):
        if name == 'swarm_agents':
            return _clean(self.monitor.list_sessions(limit=100)) if self.monitor else {'sessions': []}
        if name == 'swarm_context':
            from .pi_monitor import context_digest
            if not self.monitor:
                raise ValueError('Session monitor unavailable')
            return _clean(context_digest(self.monitor.read_session(_text(a.get('session_id'),'session_id',128),limit=20)))
        if name in {'swarm_inbox','swarm_events'}:
            after, limit = a.get('after',0), a.get('limit',50)
            if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError('Invalid cursor or limit (1..50)')
            if name == 'swarm_inbox':
                rows = self.db.execute('SELECT * FROM messages WHERE "to"=? AND id>? AND state=? ORDER BY id LIMIT ?', (actor,after,'pending',limit))
                return {'messages': [dict(r) for r in rows], 'ack_semantics':'delivered means accepted into recipient context, not completed'}
            rows = self.db.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?', (after,limit))
            return {'events':[dict(r, data=json.loads(r['data'])) for r in rows]}
        if name == 'swarm_send':
            recipient = _text(a.get('to'),'to',128)
            message = _text(a.get('message'),'message')
            if recipient != 'conductor' and self.monitor:
                if recipient not in self.monitor.bridges():
                    # Exact lookup avoids silently excluding older recorded sessions from a capped catalog.
                    try:
                        self.monitor.read_session(recipient,limit=1)
                    except (ValueError,KeyError):
                        raise ValueError('Unknown recipient session') from None
            if self.db.execute('SELECT count(*) FROM messages WHERE sender=? AND created_at>?',(actor,time.time()-60)).fetchone()[0] >= 10:
                raise ValueError('Send rate limit: 10 per minute')
            parent, root, hops = a.get('parent_id'), None, 0
            if parent is not None:
                if type(parent) is not int:
                    raise ValueError('parent_id must be an integer')
                p = self.db.execute('SELECT * FROM messages WHERE id=?',(parent,)).fetchone()
                if not p or p['to'] != actor or p['state'] != 'delivered':
                    raise ValueError('Parent must be a delivered message to caller')
                root, hops = p['root_id'], p['hops']+1
                if hops > 4 or self.db.execute('SELECT count(*) FROM messages WHERE root_id=?',(root,)).fetchone()[0] >= 20:
                    raise ValueError('Causal message budget exceeded')
            ident = self.db.execute('INSERT INTO messages(sender,"to",message,parent_id,root_id,hops,state,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                    (actor,recipient,message,parent,root,hops,'pending',time.time())).lastrowid
            event = self._event('message_sent',actor,{'message_id':ident,'to':recipient})
            self.db.execute('UPDATE messages SET event_id=?,root_id=? WHERE id=?',(event,root or ident,ident))
            return dict(self.db.execute('SELECT * FROM messages WHERE id=?',(ident,)).fetchone())
        if name == 'swarm_ack':
            ident = a.get('message_id')
            if type(ident) is not int:
                raise ValueError('message_id must be an integer')
            row = self.db.execute('SELECT * FROM messages WHERE id=?',(ident,)).fetchone()
            if not row or row['to'] != actor:
                raise ValueError('Only recipient may acknowledge message')
            if row['state'] != 'delivered':
                self.db.execute('UPDATE messages SET state=? WHERE id=?',('delivered',ident))
                self._event('message_delivered',actor,{'message_id':ident})
            return {'message_id':ident,'state':'delivered','completed':False}
        if name == 'swarm_publish':
            summary = _text(a.get('summary'),'summary')
            sources = a.get('sources',[])
            if not isinstance(sources,list) or len(sources)>20:
                raise ValueError('sources must be at most 20 strings')
            sources = [_text(s,'source',1000) for s in sources]
            ident = self.db.execute('INSERT INTO findings(actor,summary,sources,created_at) VALUES(?,?,?,?)',(actor,summary,json.dumps(sources),time.time())).lastrowid
            event = self._event('finding_published',actor,{'finding_id':ident,'summary':summary,'sources':sources})
            return {'id':ident,'event_id':event,'actor':actor,'summary':summary,'sources':sources}
        if name == 'swarm_task':
            action = a.get('action')
            if action == 'list':
                return {'tasks':self._tasks()}
            task_id = _text(a.get('task_id'),'task_id',128)
            row = self.db.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
            if action == 'create':
                if row:
                    raise ValueError('Task already exists')
                deps = a.get('dependencies',[])
                if not isinstance(deps,list) or len(deps)>50 or any(not isinstance(d,str) for d in deps):
                    raise ValueError('dependencies must be at most 50 task IDs')
                if any(not self.db.execute('SELECT 1 FROM tasks WHERE task_id=?',(d,)).fetchone() for d in deps):
                    raise ValueError('Dependency must already exist')
                owner = a.get('owner')
                if owner is not None:
                    if actor != 'conductor':
                        raise ValueError('Only conductor may assign owners')
                    owner = _text(owner,'owner',128)
                self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task_id,_text(a.get('title'),'title',300),_clean(str(a.get('description','')))[:4000],json.dumps(list(dict.fromkeys(deps))),owner,'pending',None))
            elif action in {'claim','update'}:
                if not row:
                    raise ValueError('Unknown task')
                if action == 'claim':
                    if row['owner'] not in (None,actor) or row['status'] == 'completed':
                        raise ValueError('Task already owned or completed')
                    status = 'in_progress'
                else:
                    if row['owner'] != actor:
                        raise ValueError('Only owner may update task')
                    status = a.get('status',row['status'])
                    if status not in {'pending','in_progress','blocked','completed','failed'}:
                        raise ValueError('Invalid task status')
                    if row['status'] == 'completed' and status != 'completed':
                        raise ValueError('Completed tasks cannot reopen')
                if status in {'in_progress','completed'} and any(self.db.execute('SELECT status FROM tasks WHERE task_id=?',(d,)).fetchone()[0] != 'completed' for d in json.loads(row['dependencies'])):
                    raise ValueError('Dependencies are not complete')
                result = _text(a['result'],'result') if a.get('result') is not None else row['result']
                self.db.execute('UPDATE tasks SET owner=?,status=?,result=? WHERE task_id=?',(actor,status,result,task_id))
            else:
                raise ValueError('Unknown task action')
            record = self.db.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
            task = dict(record, dependencies=json.loads(record['dependencies']))
            self._event('task_'+action,actor,task)
            return task
        raise ValueError('Unknown tool')

    def _tasks(self):
        return [dict(r,dependencies=json.loads(r['dependencies'])) for r in self.db.execute('SELECT * FROM tasks ORDER BY rowid DESC LIMIT 200')]

    def snapshot(self):
        with self.lock:
            return {'tasks':self._tasks(), 'messages':[dict(r) for r in self.db.execute('SELECT * FROM messages ORDER BY id DESC LIMIT 100')],
                    'findings':[dict(r,sources=json.loads(r['sources'])) for r in self.db.execute('SELECT * FROM findings ORDER BY id DESC LIMIT 100')],
                    'events':self._call('conductor','swarm_events',{'after':max(0,self.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]-50),'limit':50})['events'],
                    'ack_semantics':'Delivered to context does not mean task completed.'}
