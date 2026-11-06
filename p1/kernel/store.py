"""Transactional state store: SQLite tables persist artifacts, evidence, tasks, approvals, decisions,
budgets, LLM calls, and events; large payloads are stored content-addressed under blobs/ab/abcdef...
Events are inserted inside the enclosing transaction and broadcast to listeners only after commit.
"""
import json
import os
import sqlite3
import threading

from ..util import canonical, now, sha256

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, kind TEXT, size INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS nodes(hash TEXT PRIMARY KEY, kind TEXT, goal TEXT, case_id TEXT,
  inputs TEXT, meta TEXT, created REAL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, kind TEXT,
  context TEXT, status TEXT, summary TEXT, detail TEXT, goal TEXT, case_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS evidence_head(subject TEXT, kind TEXT, context TEXT, evidence INTEGER,
  PRIMARY KEY(subject, kind, context));
CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY, title TEXT, spec TEXT, status TEXT, mode TEXT,
  paused INTEGER DEFAULT 0, version INTEGER DEFAULT 1, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS cases(goal TEXT, case_id TEXT, ord INTEGER, label TEXT, paramlist TEXT,
  model TEXT, parent TEXT, status TEXT, PRIMARY KEY(goal, case_id));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, goal TEXT, case_id TEXT, kind TEXT, subject TEXT,
  status TEXT, attempt INTEGER, parent TEXT, input TEXT, result TEXT, error TEXT, pid INTEGER,
  run_dir TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS task_stops(task TEXT PRIMARY KEY, by TEXT, resume INTEGER DEFAULT 0, settled_to REAL);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, goal TEXT, case_id TEXT, kind TEXT, subject TEXT,
  payload TEXT, status TEXT, answer TEXT, decided_by TEXT, created REAL, answered REAL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, goal TEXT, case_id TEXT, kind TEXT, subject TEXT,
  question TEXT, options TEXT, status TEXT, answer TEXT, created REAL, answered REAL);
CREATE TABLE IF NOT EXISTS budget(goal TEXT PRIMARY KEY, limits TEXT, used TEXT, warned TEXT);
CREATE TABLE IF NOT EXISTS budget_entries(id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, task TEXT,
  reserved TEXT, settled TEXT, created REAL, closed REAL);
CREATE TABLE IF NOT EXISTS llm_calls(id TEXT PRIMARY KEY, goal TEXT, purpose TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, seconds REAL, status TEXT, request TEXT,
  response TEXT, created REAL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT, goal TEXT,
  params TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY, goal TEXT, seq INTEGER, reason TEXT,
  state TEXT, created REAL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, goal TEXT, status TEXT, created REAL, last_seq INTEGER);
CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, role TEXT, text TEXT,
  meta TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS run_cache(fingerprint TEXT PRIMARY KEY, run_node TEXT, created REAL);
CREATE TABLE IF NOT EXISTS documents(hash TEXT PRIMARY KEY, name TEXT, kind TEXT, size INTEGER, pages INTEGER,
  meta TEXT, created REAL);
CREATE TABLE IF NOT EXISTS list_checks(id TEXT PRIMARY KEY, goal TEXT, grp TEXT, model TEXT, effort TEXT, state TEXT, node TEXT,
  error TEXT, errors TEXT, created REAL, finished REAL, hidden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS list_corrections(id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, family TEXT, problem TEXT, docs TEXT,
  model TEXT, baseline TEXT, final TEXT, rows TEXT, checks TEXT, created REAL);
CREATE TABLE IF NOT EXISTS talks(id TEXT PRIMARY KEY, title TEXT, archived INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS rule_edits(id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, version INTEGER, rule TEXT, action TEXT,
  before TEXT, after TEXT, via TEXT, created REAL);
CREATE INDEX IF NOT EXISTS ev_subject ON evidence(subject);
CREATE INDEX IF NOT EXISTS ev_goal ON evidence(goal);
CREATE INDEX IF NOT EXISTS task_goal ON tasks(goal);
"""

J = json.dumps


def jl(s):
    return json.loads(s) if s else None


class Store:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(os.path.join(self.root, 'blobs'), exist_ok=True)
        os.makedirs(os.path.join(self.root, 'runs'), exist_ok=True)
        self.db_path = os.path.join(self.root, 'state.sqlite3')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._tx_depth = 0
        self._pending = []
        self.listeners = []
        self.cv = threading.Condition(self.lock)

    # ------------------------------------------------------------ Transactions & Events
    def tx(self):
        store = self

        class _Tx:
            def __enter__(self_):
                store.lock.acquire()
                try:
                    if store._tx_depth == 0:
                        store.db.execute('BEGIN IMMEDIATE')
                except BaseException:
                    store.lock.release()
                    raise
                store._tx_depth += 1
                return store

            def __exit__(self_, et, ev, tb):
                store._tx_depth -= 1
                try:
                    if store._tx_depth == 0:
                        if et is None:
                            try:
                                store.db.execute('COMMIT')
                            except BaseException:
                                store._pending = []
                                if store.db.in_transaction:
                                    store.db.execute('ROLLBACK')
                                raise
                            pend, store._pending = store._pending, []
                            if pend:
                                store.cv.notify_all()
                                for fn in store.listeners:
                                    for e in pend:
                                        fn(e)
                        else:
                            store.db.execute('ROLLBACK')
                            store._pending = []
                finally:
                    store.lock.release()
                return False
        return _Tx()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def q1(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def emit(self, method, params, goal=None):
        """Append an event row (must be called inside tx()); broadcast to SSE and listeners after commit."""
        cur = self.db.execute('INSERT INTO events(method, goal, params, ts) VALUES (?,?,?,?)',
                              (method, goal, J(params, ensure_ascii=False), now()))
        e = {'seq': cur.lastrowid, 'method': method, 'goal': goal, 'params': params}
        self._pending.append(e)
        return e['seq']

    def events_after(self, seq, limit=500):
        rows = self.q('SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?', (seq, limit))
        for r in rows:
            r['params'] = jl(r['params'])
        return rows

    def last_seq(self):
        r = self.q1('SELECT MAX(seq) AS s FROM events')
        return r['s'] or 0

    def wait_events(self, seq, timeout=15.0):
        with self.cv:
            if self.last_seq() <= seq:
                self.cv.wait(timeout)
        return self.events_after(seq)

    # ------------------------------------------------------------ Content Addressing
    def blob_path(self, h):
        return os.path.join(self.root, 'blobs', h[:2], h)

    def put_blob(self, data, kind='json'):
        if not isinstance(data, (bytes, str)):
            data = canonical(data)
        if isinstance(data, str):
            data = data.encode('utf-8')
        h = sha256(data)
        p = self.blob_path(h)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f'{p}.{os.getpid()}.{threading.get_ident()}.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, p)
            self.x('INSERT OR IGNORE INTO blobs VALUES (?,?,?,?)', (h, kind, len(data), now()))
        return h

    def get_blob(self, h, as_json=True):
        with open(self.blob_path(h), 'rb') as f:
            data = f.read()
        return json.loads(data) if as_json else data.decode('utf-8')

    def put_node(self, kind, content, goal=None, case_id=None, inputs=None, meta=None, text=False):
        h = self.put_blob(content, kind='text' if text else 'json')
        if not self.q1('SELECT hash FROM nodes WHERE hash=?', (h,)):
            self.x('INSERT INTO nodes VALUES (?,?,?,?,?,?,?)',
                   (h, kind, goal, case_id, J(inputs or {}), J(meta or {}, ensure_ascii=False), now()))
            self.emit('artifact/registered', {'hash': h, 'kind': kind, 'case': case_id, 'inputs': inputs or {},
                                              'meta': meta or {}}, goal=goal)
        return h

    def node(self, h):
        r = self.q1('SELECT * FROM nodes WHERE hash=?', (h,))
        if r:
            r['inputs'], r['meta'] = jl(r['inputs']), jl(r['meta'])
        return r

    # ------------------------------------------------------------ Evidence
    def put_evidence(self, subject, kind, context, status, summary, detail=None, goal=None, case_id=None):
        dh = self.put_blob(detail) if detail is not None else None
        head = self.head(subject, kind, context)
        if head and head['status'] == status and head['detail'] == dh:
            return head['id']
        cur = self.x('INSERT INTO evidence(subject, kind, context, status, summary, detail, goal, case_id, created)'
                     ' VALUES (?,?,?,?,?,?,?,?,?)',
                     (subject, kind, context, status, summary, dh, goal, case_id, now()))
        eid = cur.lastrowid
        self.x('INSERT OR REPLACE INTO evidence_head VALUES (?,?,?,?)', (subject, kind, context, eid))
        self.emit('evidence/updated', {'id': eid, 'subject': subject, 'kind': kind, 'context': context,
                                       'status': status, 'summary': summary, 'case': case_id}, goal=goal)
        return eid

    def head(self, subject, kind, context):
        r = self.q1('SELECT e.* FROM evidence_head h JOIN evidence e ON e.id = h.evidence '
                    'WHERE h.subject=? AND h.kind=? AND h.context=?', (subject, kind, context))
        return r

    def evidence(self, eid):
        r = self.q1('SELECT * FROM evidence WHERE id=?', (eid,))
        if r and r['detail']:
            r['detail_json'] = self.get_blob(r['detail'])
        return r
