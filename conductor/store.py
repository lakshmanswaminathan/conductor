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

    def user_context(self):
        return [dict(r) for r in self.db.execute("SELECT id,body FROM events WHERE source='user' AND kind='instruction' ORDER BY id")]

    def deliver_user_instructions(self, agent):
        """Append each new instruction once, at a complete exchange boundary."""
        cursor_key = 'delivered_user_event:' + agent
        with self.db:
            row = self.db.execute('SELECT value FROM config WHERE key=?', (cursor_key,)).fetchone()
            cursor = json.loads(row['value']) if row else 0
            pending = self.db.execute("SELECT id,body,at FROM events WHERE source='user' AND kind='instruction' AND id>? ORDER BY id", (cursor,)).fetchall()
            for event in pending:
                self.db.execute('INSERT INTO turns(agent,message,at) VALUES (?,?,?)',
                    (agent, encode({'role': 'user', 'content': event['body']}), event['at']))
            if pending:
                self.db.execute('INSERT OR REPLACE INTO config VALUES (?,?)', (cursor_key, encode(pending[-1]['id'])))

    def inbox(self, agent):
        return [dict(r) for r in self.db.execute(
            "SELECT id,source,body FROM events WHERE target IN (?, '*') AND kind='message' ORDER BY id", (agent,))]

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
