"""SQLite is authoritative; terminal output is only a view of the run."""
import json
import sqlite3
import time
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, parent TEXT, task TEXT NOT NULL, deps TEXT NOT NULL,
            state TEXT NOT NULL, cwd TEXT, result TEXT, heartbeat REAL, attempt INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            target TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, message TEXT NOT NULL, at REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,
            through_id INTEGER NOT NULL, summary TEXT NOT NULL, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value TEXT NOT NULL,
            source TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL,
            UNIQUE(key, value, source));
        """)
        if 'at' not in {r['name'] for r in self.db.execute('PRAGMA table_info(turns)')}:
            self.db.execute('ALTER TABLE turns ADD COLUMN at REAL NOT NULL DEFAULT 0')
            self.db.commit()

        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(events)')}
        for name in ('request_id', 'task_key', 'revision'):
            if name not in columns:
                self.db.execute(f'ALTER TABLE events ADD COLUMN {name} TEXT')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS feedback_request ON events(request_id) WHERE request_id IS NOT NULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS feedback_resolutions (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, event_id INTEGER NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def config(self):
        return {r['key']: json.loads(r['value']) for r in self.db.execute('SELECT * FROM config')}

    def configure(self, **values):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO config VALUES (?, ?)',
                                [(k, encode(v)) for k, v in values.items()])

    def event(self, source, target, kind, body):
        with self.db:
            cur = self.db.execute('INSERT INTO events(source,target,kind,body,at) VALUES (?,?,?,?,?)',
                                  (source, target, kind, body, time.time()))
        return cur.lastrowid

    def add_feedback(self, body, request_id, task_key=None, revision=None):
        """Trusted ingress only: delegated feedback never grants human authority."""
        if not isinstance(request_id, str) or not request_id or not isinstance(body, str) or not body.strip():
            raise ValueError('Feedback requires a stable request_id and body')
        revision = str(revision) if revision is not None else None
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO events(source,target,kind,body,at,request_id,task_key,revision) VALUES (?,?,?,?,?,?,?,?)',
                            ('codex', '*', 'agent_feedback', body, time.time(), request_id, task_key, revision))
            row = self.db.execute('SELECT * FROM events WHERE request_id=?', (request_id,)).fetchone()
            if (row['body'], row['task_key'], row['revision']) != (body, task_key, revision):
                raise ValueError('request_id belongs to different feedback')
            return row['id']

    def resolve_feedback(self, request_id, evidence, outcome='resolved'):
        """Archive explicitly handled feedback; immutable ingress remains retrievable."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('A feedback request_id is required')
        if outcome not in {'resolved', 'superseded'}:
            raise ValueError('Feedback outcome must be resolved or superseded')
        if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 20 or
                any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in evidence)):
            raise ValueError('Feedback resolution requires 1–20 evidence references of 1–1000 characters')
        record = {'request_id': request_id, 'evidence': evidence, 'outcome': outcome}
        payload = encode(record)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT * FROM feedback_resolutions WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['payload'] != payload:
                    raise ValueError('Feedback already has a different resolution')
                return record | {'event_id': previous['event_id']}
            if not any(row['request_id'] == request_id for row in self.feedback_context()):
                raise ValueError('Unknown feedback request_id')
            event = self.db.execute("INSERT INTO events(source,target,kind,body,at) VALUES ('coordinator','*','feedback_resolution',?,?)",
                                    (payload, time.time())).lastrowid
            self.db.execute('INSERT INTO feedback_resolutions VALUES (?,?,?)', (request_id, payload, event))
        return record | {'event_id': event}

    def active_inbox(self, agent):
        resolved = {row[0] for row in self.db.execute('SELECT request_id FROM feedback_resolutions')}
        return [row for row in self.inbox(agent) if row.get('kind') != 'feedback_resolution'
                and row.get('request_id') not in resolved]

    def feedback_context(self):
        return self._legacy_feedback() + [dict(r) for r in self.db.execute("SELECT id,source,body,request_id,task_key,revision FROM events WHERE kind='agent_feedback' ORDER BY id")]

    def feedback_version(self):
        return max((row['id'] for row in self.feedback_context()), default=0)

    def _legacy_feedback(self):
        # Only a matching persisted mailbox record proves transport provenance.
        # Keep the original request pinned: it may contain the only goal record.
        rows = list(self.db.execute("SELECT id,body FROM events WHERE source='user' AND kind='instruction' ORDER BY id"))
        mailbox = self.root.parent.parent / 'conductor-mailbox.sqlite3'
        if len(rows) < 2 or not mailbox.is_file():
            return []
        from .conversation_labels import _HEADER, _FOOTER
        db = sqlite3.connect(mailbox.as_uri() + '?mode=ro', uri=True)
        try:
            known = {body: request for request, body in db.execute('SELECT request_id,message FROM requests')}
        finally:
            db.close()
        return [dict(row) | {'source': 'codex', 'request_id': known[row['body']], 'task_key': None, 'revision': None}
                for row in rows[1:] if row['body'] in known and _HEADER.match(row['body']) and row['body'].endswith(_FOOTER)]

    def instruction_events(self):
        return [dict(r) for r in self.db.execute("SELECT id,body,kind,request_id FROM events WHERE (source='user' AND kind='instruction') OR kind='agent_feedback' ORDER BY id")]

    def user_context(self):
        legacy = {row['id'] for row in self._legacy_feedback()}
        return [dict(r) for r in self.db.execute("SELECT id,body FROM events WHERE source='user' AND kind='instruction' ORDER BY id") if r['id'] not in legacy]

    def deliver_user_instructions(self, agent):
        """Append each new instruction once, at a complete exchange boundary."""
        cursor_key = 'delivered_user_event:' + agent
        with self.db:
            row = self.db.execute('SELECT value FROM config WHERE key=?', (cursor_key,)).fetchone()
            cursor = json.loads(row['value']) if row else 0
            pending = self.db.execute("SELECT id,body,at FROM events WHERE source='user' AND kind='instruction' AND id>? ORDER BY id", (cursor,)).fetchall()
            human_ids = {row['id'] for row in self.user_context()}
            for event in pending:
                if event['id'] not in human_ids:
                    continue
                self.db.execute('INSERT INTO turns(agent,message,at) VALUES (?,?,?)',
                    (agent, encode({'role': 'user', 'content': event['body']}), event['at']))
            if pending:
                self.db.execute('INSERT OR REPLACE INTO config VALUES (?,?)', (cursor_key, encode(pending[-1]['id'])))

    def inbox(self, agent):
        return self._legacy_feedback() + [dict(r) for r in self.db.execute(
            "SELECT id,source,kind,body,request_id,task_key,revision FROM events WHERE target IN (?, '*') AND kind IN ('message','agent_feedback','feedback_resolution') ORDER BY id", (agent,))]

    def agents(self):
        return [dict(r) | {'deps': json.loads(r['deps'])} for r in self.db.execute('SELECT * FROM agents ORDER BY rowid')]

    def agent(self, ident):
        return next((a for a in self.agents() if a['id'] == ident), None)

    def add_agent(self, ident, parent, task, deps=()):
        if not ident or len(ident) > 48 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in ident):
            raise ValueError('Agent IDs must be 1–48 lowercase letters, digits, underscores or hyphens')
        if self.agent(ident):
            raise ValueError('Agent ID already exists')
        if any(not self.agent(d) or d == ident or d == 'coordinator' for d in deps):
            raise ValueError('Dependencies must name existing worker tasks')
        with self.db:
            self.db.execute('INSERT INTO agents(id,parent,task,deps,state) VALUES (?,?,?,?,?)',
                            (ident, parent, task, encode(list(dict.fromkeys(deps))), 'pending'))

    def update_agent(self, ident, **values):
        if not values or not set(values) <= {'state', 'cwd', 'result', 'heartbeat', 'attempt'}:
            raise ValueError('Invalid agent update')
        with self.db:
            self.db.execute('UPDATE agents SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?',
                            (*values.values(), ident))

    def claim_worker(self, ident):
        with self.db:
            result = self.db.execute("UPDATE agents SET state='running',heartbeat=? WHERE id=? AND state='starting'", (time.time(), ident))
        return result.rowcount == 1

    def append(self, agent, message):
        with self.db:
            self.db.execute('INSERT INTO turns(agent,message,at) VALUES (?,?,?)', (agent, encode(message), time.time()))

    def history(self, agent, after=0):
        return [(r['id'], json.loads(r['message'])) for r in self.db.execute(
            'SELECT id,message FROM turns WHERE agent=? AND id>? ORDER BY id', (agent, after))]

    def checkpoint(self, agent):
        r = self.db.execute('SELECT * FROM checkpoints WHERE agent=? ORDER BY id DESC LIMIT 1', (agent,)).fetchone()
        return dict(r) if r else {'through_id': 0, 'summary': ''}

    def save_checkpoint(self, agent, through, summary):
        with self.db:
            self.db.execute('INSERT INTO checkpoints(agent,through_id,summary,at) VALUES (?,?,?,?)',
                            (agent, through, summary, time.time()))

    def claims(self):
        return [dict(r) | {'evidence': json.loads(r['evidence'])} for r in self.db.execute(
            "SELECT * FROM claims WHERE status != 'superseded' ORDER BY key,id")]

    def merge(self, source, findings):
        """Idempotent, provenance-preserving merge. Different values remain disputed."""
        if not self.agent(source):
            raise ValueError('Unknown source agent')
        if not isinstance(findings, list) or len(findings) > 20:
            raise ValueError('Provide at most 20 findings')
        for f in findings:
            if not isinstance(f, dict) or not isinstance(f.get('key'), str) or not f['key'] or len(f['key']) > 120:
                raise ValueError('Each finding needs a short stable key')
            if not isinstance(f.get('value'), str) or not f['value'] or len(f['value']) > 4000:
                raise ValueError('Each finding needs a value of 1–4000 characters')
            if not isinstance(f.get('evidence'), list) or not all(isinstance(x, str) for x in f['evidence']):
                raise ValueError('Each finding needs an evidence array')
        with self.db:
            for f in findings:
                self.db.execute('INSERT OR IGNORE INTO claims(key,value,source,evidence,status) VALUES (?,?,?,?,?)',
                                (f['key'], f['value'], source, encode(f['evidence']), 'reported'))
                values = self.db.execute("SELECT DISTINCT value FROM claims WHERE key=? AND status!='superseded'", (f['key'],)).fetchall()
                if len(values) > 1:
                    self.db.execute("UPDATE claims SET status='conflict' WHERE key=? AND status!='superseded' AND source!='user'", (f['key'],))

    def resolve(self, key, value):
        """Only the user-facing CLI calls this; agents cannot overwrite user decisions."""
        with self.db:
            self.db.execute("UPDATE claims SET status='superseded' WHERE key=?", (key,))
            self.db.execute("INSERT INTO claims(key,value,source,evidence,status) VALUES (?,?,'user','[]','accepted') ON CONFLICT(key,value,source) DO UPDATE SET status='accepted'", (key, value))
            self.db.execute("INSERT INTO events(source,target,kind,body,at) VALUES ('user','*','instruction',?,?)",
                            (f'Decision for {key}: {value}', time.time()))
