#!/usr/bin/env python3
"""Compute node coordinator: node registration & heartbeats, shard claiming & leases,
content-addressed blob store, result collection, and bearer-authenticated admin API.

Two job types are supported:
- Sharded jobs: Reference MC split across disjoint history ranges, OpenMC split across seeds.
- Whole-case jobs: single shard running unmodified criticality inputs, with OpenMC assigned a
  dynamic thread range [min_threads, threads], live progress heartbeats, and statepoint checkpoints.

Usage: python3 -I p1/nodes/coord.py --var ~/pmx-coord --host 10.0.0.10 --port 8991

var directory layout:
  tokens.json   node_name -> bearer token (mode 0600)
  admin.token   admin bearer token (auto-generated on startup if missing, mode 0600)
  probe.json    local solver probe config matching node.json solvers section
  state.sqlite3, blobs/
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__:
    from . import jobs as J
else:
    # When invoked via `python3 -I`, the script directory is omitted from sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import jobs as J  # noqa: E402

PROTO = 4              # Node protocol version: agents with mismatched version are rejected at registration
LEASE_S = 120
# Agents self-terminate active runs after RENEW_LIMIT_S without coordinator contact; coordinator uses
# the same window (+ 2 heartbeats margin) before treating an unreachable node's run as stopped.
RENEW_LIMIT_S = 600
# Whole-case jobs created in held state await backend activation; auto-cancel if never activated.
HELD_TTL_S = 600
HB_S = 15              # Default heartbeat interval; node marked offline after 2 * hb_s without contact
TAIL_FASTER = 1.25     # Speed ratio defining a faster peer for tail-latency straggler avoidance
DUP_GAIN = 1.5         # Speculative duplication factor when remaining holder time exceeds peer solve time
MAX_COPIES = 2
FAIL_LIMIT = 3         # Max shard failures before marking shard failed
# Whole-case failures return immediately to the kernel to surface in decisions/retries
CASE_FAIL_LIMIT = 1
CKPT_S = 120           # Minimum interval (s) between uploaded OpenMC statepoints
# Replicate a whole-case shard onto another node if its current holder has been continuously suspended
# for SUSPEND_DUP_S (tracks both whole-node suspend and partial per-process SIGSTOP via coordinator clock).
SUSPEND_DUP_S = 180
# Whole-case jobs are dispatched largest-est first (in baseline core-seconds) using each node's measured
# per-thread speed so heavier runs land on the node expected to finish them soonest.
CASE_SETTLE_S = 2.0      # Brief batching settle window so concurrently submitted cases sort together
CASE_SETTLE_MAX_S = 10.0 # Maximum dispatch delay during continuous job submissions
CASE_SPEED_DEFAULT = 0.5 # Conservative initial per-thread speed (baseline core-s / node thread-s)
CASE_SPEED_RECENT = 8    # Median window over recent whole-case completions per solver
# If every job finishes sooner elsewhere, still claim locally when local finish <= CASE_IDLE_RATIO * best
CASE_IDLE_RATIO = 1.25
# Deadline slack after expected remote start before reclaiming a yielded whole-case job
CASE_YIELD_SLACK = 0.5
CASE_YIELD_MIN_S = 60.0
# Overdue whole-case remaining-time fallback factor when progress lines are unavailable
CASE_OVERDUE = 0.5
HOLD_MAX_S = 3600
PLAN_MAX = 200           # Max hypothetical jobs accepted per /admin/plan request
JOB_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS nodes(name TEXT PRIMARY KEY, caps TEXT, first_seen REAL, last_seen REAL, addr TEXT,
    enabled INTEGER NOT NULL DEFAULT 1, state TEXT, res TEXT, proto INTEGER, reject TEXT, rescan INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, solver TEXT, spec TEXT, created REAL);
CREATE TABLE IF NOT EXISTS shards(job TEXT, idx INTEGER, spec TEXT, state TEXT, node TEXT, work REAL,
    attempts INTEGER NOT NULL DEFAULT 0, fails INTEGER NOT NULL DEFAULT 0, outputs TEXT, cpu_s REAL, wall_s REAL,
    claimed REAL, finished REAL, rc INTEGER, PRIMARY KEY(job, idx));
CREATE TABLE IF NOT EXISTS leases(job TEXT, idx INTEGER, node TEXT, until REAL, claimed REAL, PRIMARY KEY(job, idx, node));
CREATE INDEX IF NOT EXISTS leases_node ON leases(node);
CREATE TABLE IF NOT EXISTS ranges(job TEXT PRIMARY KEY, start INTEGER, count INTEGER);
CREATE TABLE IF NOT EXISTS events(t REAL, node TEXT, job TEXT, idx INTEGER, what TEXT);
CREATE TABLE IF NOT EXISTS execs(lease TEXT PRIMARY KEY, job TEXT, idx INTEGER, node TEXT, threads INTEGER, claimed REAL,
    seen REAL, suspended_s REAL, start_batch INTEGER, ended REAL, why TEXT, wall_s REAL, rc INTEGER, inputs TEXT);
CREATE INDEX IF NOT EXISTS execs_job ON execs(job, idx);
'''
# Schema migrations for existing SQLite databases across protocol upgrades
COLUMNS = {'jobs': [('kind', 'TEXT'), ('yield_until', 'REAL'), ('activated', 'REAL')],
           'execs': [('claim_seq', 'INTEGER')],
           'shards': [('last_run', 'REAL'), ('ckpt', 'TEXT'), ('exec', 'TEXT'), ('suspended_s', 'REAL')],
           'leases': [('threads', 'INTEGER NOT NULL DEFAULT 1'), ('mem', 'REAL'), ('progress', 'TEXT'), ('seen', 'REAL'),
                      ('id', 'TEXT'), ('inputs', 'TEXT'), ('start_batch', 'INTEGER'), ('susp_from', 'REAL')],
           'nodes': [('running', 'TEXT'), ('hold_until', 'REAL'), ('hold_why', 'TEXT'), ('susp_from', 'REAL'),
                     ('free', 'INTEGER'), ('capacity', 'INTEGER'), ('epoch', 'INTEGER'),
                     ('hb_seq', 'INTEGER NOT NULL DEFAULT 0'), ('claim_seq', 'INTEGER NOT NULL DEFAULT 0')]}


def _mem_total(row):
    """Node memory limit reported in last heartbeat, or None if unreported."""
    try:
        return json.loads(row['res'] or '{}').get('mem_bytes')
    except (ValueError, TypeError):
        return None


def _steps(jspec):
    """Total steps for a whole-case job (OpenMC batches or Reference MC KCODE cycles), or None."""
    return (jspec.get('plan') or {}).get('steps')


def _partial_free(row):
    """Claimable backfill capacity during partial suspension; returns None when unlimited."""
    host = (json.loads(row['res'] or '{}').get('host') or {})
    if row['free'] is not None and (row['capacity'] is not None or host.get('suspended')):
        return max(0, row['free'])
    return None


def _scaled_est(jspec, start_batch, spec_batch=0):
    """Scale remaining work estimate when resuming from start_batch after spec_batch."""
    est, n = jspec.get('est'), _steps(jspec)
    if not est or not start_batch or not n or start_batch <= (spec_batch or 0):
        return est
    left0 = n - (spec_batch or 0)
    return est * max(0.0, n - start_batch) / left0 if left0 > 0 else est


class CasePlan:
    """Schedules whole-case jobs in descending est order across peer nodes based on projected
    slot release times, per-thread solver speed, and memory headroom (in seconds from now)."""

    def __init__(self, co, c, node, now, live):
        self.co, self.c, self.node, self.now, self.live = co, c, node, now, live
        self.speeds = {}
        self.slots = {}
        self.mem = {}           # node -> (mem_limit_or_None, [(free_at_s, mem_bytes)])
        for y, (caps, row) in live.items():
            if y == node:
                continue
            busy, held = [], []
            for l in c.execute('SELECT l.threads, l.claimed, l.progress, l.mem, l.start_batch, l.susp_from, j.spec, j.solver, s.spec AS sspec '
                               'FROM leases l JOIN jobs j ON j.id=l.job JOIN shards s ON s.job=l.job AND s.idx=l.idx '
                               'WHERE l.node=?', (y,)):
                js = json.loads(l['spec'])
                left = self._left(y, js, l)
                busy += [left] * (l['threads'] or 1)
                if l['mem']:
                    held.append((left, l['mem']))
            n = caps.get('slots', 1)
            self.slots[y] = sorted(busy + [0.0] * max(0, n - len(busy)))[:max(n, len(busy))]
            limited = _partial_free(row)
            if limited is not None:
                self.slots[y] = [0.0] * min(n, limited)
            self.mem[y] = (_mem_total(row), held)

    def speed(self, y, solver):
        if solver not in self.speeds:
            self.speeds[solver] = Coordinator.case_speeds(self.c, solver)
        return self.speeds[solver].get(y, CASE_SPEED_DEFAULT)

    def _left(self, y, js, l):
        """Estimate remaining seconds on an active lease using est, live batch progress, or overdue factor."""
        pr = json.loads(l['progress']) if l['progress'] else {}
        if pr.get('suspended') or l['susp_from']:
            return float('inf')
        if js.get('kind') != 'case':
            return float(LEASE_S)
        ran = max(0.0, self.now - l['claimed'] - (pr.get('suspended_s') or 0))
        est = _scaled_est(js, l['start_batch'], json.loads(l['sspec']).get('restart_batch', 0)) if js.get('est') else None
        if est:
            exp = est / (self.speed(y, l['solver']) * (l['threads'] or 1))
            if ran < exp:
                return max(5.0, exp - ran)
        done, n = pr.get('batch') or pr.get('cycle'), _steps(js)
        start = l['start_batch'] or 0
        if done and n and done > start and ran > 0:
            return max(5.0, ran * (n - done) / (done - start))
        return max(5.0, CASE_OVERDUE * ran) if est else float(LEASE_S)

    def _start(self, y, lo, need):
        """Earliest time on node y when lo slots and need bytes of memory are simultaneously free, or None."""
        sl = self.slots[y]
        total, held = self.mem[y]
        if len(sl) < lo or (need and total is not None and need > total):
            return None
        for t in sorted({0.0, *sl, *(f for f, _ in held)}):
            if t == float('inf'):
                continue
            if sum(1 for v in sl if v <= t) < lo:
                continue
            if need and total is not None and total - sum(m for f, m in held if f > t) < need:
                continue
            return t
        return None

    def best(self, jspec):
        """Best peer completion estimate: (finish_s, node, threads, start_s) or None if no peer fits."""
        est, solver = jspec['est'], jspec['solver']
        lo, hi = jspec['threads']
        need = jspec.get('mem')
        best = None
        for y, sl in self.slots.items():
            if not self.co._eligible(jspec, y, self.live[y][0]):
                continue
            start = self._start(y, lo, need)
            if start is None:
                continue
            ty = min(hi, sum(1 for v in sl if v <= start))
            f = start + est / (self.speed(y, solver) * ty)
            if best is None or f < best[0]:
                best = (f, y, ty, start)
        return best

    def take(self, jspec, best):
        """Reserve slots and memory on the winning node in the schedule simulation."""
        f, y, ty, start = best
        # Earliest ty slots are <= start; advance them to completion time f
        self.slots[y] = sorted([f] * ty + self.slots[y][ty:])
        if jspec.get('mem'):
            self.mem[y][1].append((f, jspec['mem']))

    def offer(self, jspec, t):
        """Return None if claiming node finishes fastest; otherwise reserve on best peer and return (ratio, start)."""
        mine = jspec['est'] / (self.speed(self.node, jspec['solver']) * t)
        best = self.best(jspec)
        if best is None or mine <= best[0]:
            return None
        self.take(jspec, best)
        return mine / best[0], best[3]


class HttpError(Exception):
    def __init__(self, code, msg, commit=False):
        super().__init__(msg)
        self.code, self.msg = code, msg
        self.commit = commit


def matches(spec, caps):
    """Check that node executable sha256 and all required nuclear data file sha256s match."""
    req = spec['requires']
    have = (caps.get('solvers') or {}).get(spec['solver'])
    if not have or have.get('exe') != req['exe']:
        return False
    files = have.get('files', {})
    return all(files.get(k) == v for k, v in req['files'].items())


def missing_files(spec, caps):
    have = ((caps.get('solvers') or {}).get(spec['solver']) or {}).get('files', {})
    return sorted(k for k, v in spec['requires']['files'].items() if have.get(k) != v)


class Coordinator:
    def __init__(self, var, probe_ref_mc=None):
        self.var = Path(var).expanduser()
        self.blobs = self.var / 'blobs'
        self.lock = threading.Lock()
        self._tok = (None, {})
        self.probe_ref_mc = probe_ref_mc or J.ref_mc_probe
        # Cache Reference MC file probes by normalized deck hash
        self._probes = {}
        self.init()

    # ---------- Storage ----------
    def db(self):
        c = sqlite3.connect(self.var / 'state.sqlite3', timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        return c

    def init(self):
        self.blobs.mkdir(parents=True, exist_ok=True)
        c = self.db()
        c.executescript(SCHEMA)
        for table, cols in COLUMNS.items():
            have = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            for name, decl in cols:
                if name not in have:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
        # Extend existing leases across coordinator restarts so active workers are not falsely expired
        c.execute('UPDATE leases SET until=MAX(until, ?)', (time.time() + 2 * HB_S + LEASE_S / 4,))
        c.close()
        p = self.var / 'admin.token'
        if not p.exists():
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(32) + '\n')

    def tx(self, fn, *a):
        with self.lock:
            c = self.db()
            try:
                c.execute('BEGIN IMMEDIATE')
                out = fn(c, *a)
                c.execute('COMMIT')
                return out
            except HttpError as e:
                c.execute('COMMIT' if e.commit else 'ROLLBACK')
                raise
            except BaseException:
                c.execute('ROLLBACK')
                raise
            finally:
                c.close()

    def read(self, fn, *a):
        c = self.db()
        try:
            return fn(c, *a)
        finally:
            c.close()

    def tokens(self):
        """Reload tokens.json on mtime change so nodes can be added without restarting coordinator."""
        p = self.var / 'tokens.json'
        try:
            m = p.stat().st_mtime_ns
        except FileNotFoundError:
            return {}
        if self._tok[0] != m:
            self._tok = (m, json.loads(p.read_text()))
        return self._tok[1]

    def admin_token(self):
        return (self.var / 'admin.token').read_text().strip()

    def blob_path(self, h):
        return self.blobs / h[:2] / h

    def put_blob(self, data):
        h = hashlib.sha256(data).hexdigest()
        p = self.blob_path(h)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.parent / f'{h}.{threading.get_ident()}.part'
            tmp.write_bytes(data)
            tmp.replace(p)
        return h

    @staticmethod
    def event(c, node, job, idx, what):
        c.execute('INSERT INTO events VALUES(?,?,?,?,?)', (time.time(), node, job, idx, what))

    # ---------- Node Protocol ----------
    def register(self, c, node, b, addr, now):
        caps = b['caps']
        epoch = b.get('epoch')
        old = c.execute('SELECT epoch FROM nodes WHERE name=?', (node,)).fetchone()
        if old and old['epoch'] is not None:
            if not isinstance(epoch, int) or epoch < old['epoch']:
                raise HttpError(409, 'node instance superseded')
            if epoch == old['epoch']:
                c.execute('UPDATE nodes SET caps=?, last_seen=MAX(last_seen, ?), addr=? WHERE name=?',
                          (json.dumps(caps), now, addr, node))
                return {'ok': True, 'lease_s': LEASE_S, 'proto': PROTO}
        c.execute('INSERT INTO nodes(name, caps, first_seen, last_seen, addr, proto, state) VALUES(?,?,?,?,?,?,?) '
                  'ON CONFLICT(name) DO UPDATE SET caps=excluded.caps, last_seen=excluded.last_seen, addr=excluded.addr, '
                  'proto=excluded.proto, reject=NULL, state=excluded.state, res=NULL',
                  (node, json.dumps(caps), now, now, addr, PROTO, 'active'))
        c.execute('UPDATE nodes SET epoch=?, hb_seq=0 WHERE name=?', (epoch, node))
        # Fresh agent startup: prior processes on that node have exited, so release old leases
        n = self._release(c, node, 'node restarted, requeued', ended=True)
        self.event(c, node, None, None, f'register proto={PROTO} agent={caps.get("agent", {}).get("sha", "")[:12]}'
                   + (f', {n} leases released' if n else ''))
        return {'ok': True, 'lease_s': LEASE_S, 'proto': PROTO}

    def reject(self, c, node, proto, addr, now):
        why = f'Agent protocol {proto or 1} rejected; coordinator requires {PROTO}. Upgrade and restart agent.'
        c.execute('INSERT INTO nodes(name, caps, first_seen, addr, reject) VALUES(?,?,?,?,?) '
                  'ON CONFLICT(name) DO UPDATE SET reject=excluded.reject, addr=excluded.addr',
                  (node, '{}', now, addr, json.dumps({'proto': proto, 't': now, 'why': why}, ensure_ascii=False)))
        return why

    @staticmethod
    def _entries(running):
        """Parse heartbeat running list: [job, idx, lease_id?, suspended?]."""
        out = []
        for e in running or []:
            out.append((e[0], e[1], e[2] if len(e) > 2 else None, bool(e[3]) if len(e) > 3 else False))
        return out

    def heartbeat(self, c, node, b, now):
        current = c.execute('SELECT epoch, hb_seq, claim_seq FROM nodes WHERE name=?', (node,)).fetchone()
        if current and current['epoch'] is not None and b.get('epoch') != current['epoch']:
            raise HttpError(409, 'node instance superseded')
        seq, ack = b.get('seq'), b.get('acked_claim')
        ordered = isinstance(seq, int) and not isinstance(seq, bool) and seq > 0
        if ordered and current and seq <= current['hb_seq']:
            return {'cancel': [], 'rescan': False}
        if ordered:
            c.execute('UPDATE nodes SET hb_seq=? WHERE name=?', (seq, node))
        state = b.get('state') or 'active'
        entries = self._entries(b.get('running'))
        c.execute('UPDATE nodes SET last_seen=MAX(last_seen, ?), state=?, res=?, running=?, '
                  'susp_from=CASE WHEN ?=\'suspend\' THEN COALESCE(susp_from, ?) ELSE NULL END WHERE name=?',
                  (now, state, json.dumps(b.get('res') or {}), json.dumps([[j, i] for j, i, _, _ in entries]), state, now, node))
        free = b.get('free')
        cap = b.get('capacity')
        boundary = min(ack, current['claim_seq']) if ordered and current and isinstance(ack, int) and not isinstance(ack, bool) else 0
        if ordered and isinstance(free, int) and not isinstance(free, bool):
            newer = c.execute('SELECT COALESCE(SUM(l.threads),0) FROM leases l JOIN execs e ON e.lease=l.id '
                              'WHERE l.node=? AND e.claim_seq>?', (node, boundary)).fetchone()[0]
            free = max(0, free - newer)
        c.execute('UPDATE nodes SET free=?, capacity=? WHERE name=?',
                  (max(0, free) if isinstance(free, int) and not isinstance(free, bool) else None,
                   max(0, cap) if isinstance(cap, int) and not isinstance(cap, bool) else None, node))
        cancel = []
        for job, idx, lid, susp in entries:
            c.execute('UPDATE shards SET last_run=MAX(COALESCE(last_run, 0), ?) WHERE job=? AND idx=?', (now, job, idx))
            if lid:
                c.execute('UPDATE execs SET seen=MAX(seen, ?) WHERE lease=? AND node=? AND job=? AND idx=? AND ended IS NULL',
                          (now, lid, node, job, idx))
            l = c.execute('SELECT id FROM leases WHERE job=? AND idx=? AND node=?', (job, idx, node)).fetchone()
            if l and (lid is None or l['id'] in (None, lid)):
                # Track continuous suspension start using coordinator clock
                c.execute('UPDATE leases SET until=MAX(until, ?), susp_from=CASE WHEN ? THEN COALESCE(susp_from, ?) ELSE NULL END '
                          'WHERE job=? AND idx=? AND node=?', (now + LEASE_S, susp or state == 'suspend', now, job, idx, node))
            else:
                cancel.append([job, idx])
        # Close execs up to acked_claim that are no longer reported in the agent's running list
        lids = {lid for _, _, lid, _ in entries if lid}
        keys = {(j, i) for j, i, lid, _ in entries if not lid}
        for e in c.execute('SELECT lease, job, idx FROM execs WHERE node=? AND ended IS NULL AND claim_seq<=?', (node, boundary)).fetchall():
            if e['lease'] not in lids and (e['job'], e['idx']) not in keys:
                c.execute("UPDATE execs SET ended=?, why=COALESCE(why, 'not running') WHERE lease=?", (now, e['lease']))
        self._requeue_expired(c, now)
        r = c.execute('SELECT rescan FROM nodes WHERE name=?', (node,)).fetchone()
        if r and r['rescan']:
            c.execute('UPDATE nodes SET rescan=0 WHERE name=?', (node,))
        return {'cancel': cancel, 'rescan': bool(r and r['rescan']), 'claim_seq': current['claim_seq'] if current else 0}

    def update_caps(self, c, node, b, now):
        c.execute('UPDATE nodes SET caps=?, last_seen=MAX(last_seen, ?) WHERE name=?', (json.dumps(b['caps']), now, node))
        self.event(c, node, None, None, 'capabilities updated')
        return {'ok': True}

    def _requeue_expired(self, c, now):
        # Delete expired leases while keeping execs open until done or RENEW_LIMIT_S expires
        for r in c.execute('SELECT job, idx, node FROM leases WHERE until<?', (now,)).fetchall():
            c.execute('DELETE FROM leases WHERE job=? AND idx=? AND node=?', (r['job'], r['idx'], r['node']))
            self.event(c, r['node'], r['job'], r['idx'], 'lease expired')
        self._unleased_to_queue(c)
        for r in c.execute("SELECT DISTINCT s.job FROM shards s JOIN jobs j ON j.id=s.job WHERE s.state='held' AND j.created<?",
                           (now - HELD_TTL_S,)).fetchall():
            c.execute("UPDATE shards SET state='cancelled' WHERE job=? AND state='held'", (r['job'],))
            self.event(c, None, r['job'], None, f'not activated within {HELD_TTL_S} s, cancelled')

    @staticmethod
    def _unleased_to_queue(c):
        c.execute("UPDATE shards SET state='queued', node=NULL WHERE state='leased' AND NOT EXISTS "
                  "(SELECT 1 FROM leases l WHERE l.job=shards.job AND l.idx=shards.idx)")

    def _release(self, c, node, why, ended=False):
        """Revoke all active leases held by node; ended=True marks open exec records terminated."""
        rows = c.execute('SELECT job, idx FROM leases WHERE node=?', (node,)).fetchall()
        for r in rows:
            c.execute('DELETE FROM leases WHERE job=? AND idx=? AND node=?', (r['job'], r['idx'], node))
            self.event(c, node, r['job'], r['idx'], why)
        if ended:
            c.execute('UPDATE execs SET ended=?, why=COALESCE(why, ?) WHERE node=? AND ended IS NULL', (time.time(), why, node))
        self._unleased_to_queue(c)
        return len(rows)

    def _live(self, c, now):
        """Return online, enabled, active, unheld nodes: name -> (caps, row)."""
        out = {}
        for r in c.execute('SELECT * FROM nodes WHERE enabled=1 AND reject IS NULL').fetchall():
            caps = json.loads(r['caps'] or '{}')
            if r['last_seen'] and now - r['last_seen'] <= 2 * caps.get('hb_s', HB_S) and (r['state'] or 'active') == 'active' \
                    and not (r['hold_until'] and r['hold_until'] > now):
                out[r['name']] = (caps, r)
        return out

    @staticmethod
    def rates(c, job):
        """Per-slot work rate (work/s) for each node based on completed shards in job."""
        per = {}
        for r in c.execute("SELECT node, work, wall_s - COALESCE(suspended_s, 0) AS t FROM shards WHERE job=? AND state='done' "
                           "AND wall_s - COALESCE(suspended_s, 0) > 0", (job,)):
            per.setdefault(r['node'], []).append(r['work'] / r['t'])
        return {n: statistics.median(v) for n, v in per.items()}

    @staticmethod
    def case_speeds(c, solver):
        """Per-thread speed for whole-case jobs: est / (active_wall * threads), median of recent CASE_SPEED_RECENT runs."""
        per = {}
        for r in c.execute("SELECT s.node, s.wall_s, s.suspended_s, s.exec, j.spec FROM shards s JOIN jobs j ON j.id=s.job "
                           "WHERE j.kind='case' AND j.solver=? AND s.state='done' AND s.wall_s>0 ORDER BY s.finished DESC LIMIT 500",
                           (solver,)):
            js, ex = json.loads(r['spec']), json.loads(r['exec'] or '{}')
            # Scale est proportionally when the winning execution resumed from a statepoint
            est = _scaled_est(js, ex.get('restart_batch'), ex.get('spec_batch'))
            if ex.get('restart_batch') and est == js.get('est') and not _steps(js):
                continue
            act = r['wall_s'] - (r['suspended_s'] or 0)
            if not est or act <= 0 or len(per.get(r['node'], [])) >= CASE_SPEED_RECENT:
                continue
            t = ex.get('threads') or 1
            per.setdefault(r['node'], []).append(est / (act * t))
        return {n: statistics.median(v) for n, v in per.items()}

    def _eligible(self, jspec, name, caps):
        return (not jspec.get('nodes') or name in jspec['nodes']) and matches(jspec, caps)

    def _tail_ok(self, c, job, jspec, node, work, live):
        """Avoid assigning tail shards to slow nodes if faster peers can clear the remaining queue sooner."""
        r = self.rates(c, job)
        rn = r.get(node)
        if not rn:
            return True
        fast = sum(caps.get('slots', 1) * r[n] for n, (caps, _) in live.items()
                   if n != node and r.get(n, 0) > TAIL_FASTER * rn and self._eligible(jspec, n, caps))
        if not fast:
            return True
        q = c.execute("SELECT COALESCE(SUM(work), 0) FROM shards WHERE job=? AND state='queued'", (job,)).fetchone()[0]
        return work / rn <= q / fast

    def _lease(self, c, node, job, idx, now, why, threads=1, mem=None, shard=None):
        """Issue a unique lease_id and record an execution row; returns lease_id."""
        lid = secrets.token_hex(8)
        sh = shard or {}
        c.execute('INSERT INTO leases(job, idx, node, until, claimed, threads, mem, id, inputs, start_batch) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (job, idx, node, now + LEASE_S, now, threads, mem, lid, json.dumps(sh.get('inputs') or {}), sh.get('restart_batch') or 0))
        c.execute('INSERT INTO execs(lease, job, idx, node, threads, claimed, seen, suspended_s, start_batch, inputs) '
                  'VALUES(?,?,?,?,?,?,?,0,?,?)', (lid, job, idx, node, threads, now, now, sh.get('restart_batch') or 0,
                                                  json.dumps(sh.get('inputs') or {})))
        c.execute('UPDATE nodes SET claim_seq=claim_seq+1 WHERE name=?', (node,))
        c.execute('UPDATE execs SET claim_seq=(SELECT claim_seq FROM nodes WHERE name=?) WHERE lease=?', (node, lid))
        c.execute("UPDATE shards SET state='leased', attempts=attempts+1, claimed=COALESCE(claimed, ?) WHERE job=? AND idx=?",
                  (now, job, idx))
        # Clear any prior yield hold now that the job has been claimed
        c.execute('UPDATE jobs SET yield_until=NULL WHERE id=?', (job,))
        self.event(c, node, job, idx, why + (f' threads={threads}' if threads != 1 else ''))
        return lid

    def _grant(self, c, node, job, jspec, s, now, why, t):
        sh = self._shard_for(c, job, jspec, s['spec'])
        lid = self._lease(c, node, job, s['idx'], now, why, t, jspec.get('mem'), sh)
        c.execute('UPDATE nodes SET free=MAX(0, free-?) WHERE name=? AND free IS NOT NULL', (t, node))
        seq = c.execute('SELECT claim_seq FROM execs WHERE lease=?', (lid,)).fetchone()['claim_seq']
        return {'job': job, 'idx': s['idx'], 'job_spec': jspec, 'shard': sh, 'threads': t, 'lease': lid, 'claim_seq': seq}

    @staticmethod
    def _fit(jspec, free, mem_free):
        """Determine thread count for this job on the node, or None if slots/memory are insufficient."""
        if jspec.get('kind') == 'case':
            lo, hi = jspec['threads']
            t = min(hi, free)
            if t < lo:
                return None
            if mem_free is not None and jspec.get('mem') and jspec['mem'] > mem_free:
                return None
            return t
        t = jspec.get('threads', 1)
        return t if t <= free else None

    def _shard_for(self, c, job, jspec, spec):
        """When re-issuing a whole-case job, resume from the latest uploaded statepoint if newer."""
        sh = json.loads(spec)
        if jspec.get('kind') != 'case':
            return sh
        r = c.execute('SELECT ckpt FROM shards WHERE job=? AND idx=0', (job,)).fetchone()
        ck = json.loads(r['ckpt']) if r and r['ckpt'] else None
        if ck and ck['batch'] > sh.get('restart_batch', 0) and self.blob_path(ck['sha']).exists():
            inputs = {k: v for k, v in sh['inputs'].items() if k != sh.get('restart')}
            inputs[ck['name']] = ck['sha']
            sh = dict(sh, inputs=inputs, restart=ck['name'], restart_batch=ck['batch'])
        return sh

    def claim(self, c, node, now, b=None):
        instance = c.execute('SELECT epoch FROM nodes WHERE name=?', (node,)).fetchone()
        if instance and instance['epoch'] is not None and (b or {}).get('epoch') != instance['epoch']:
            raise HttpError(409, 'node instance superseded')
        c.execute('UPDATE nodes SET last_seen=MAX(last_seen, ?) WHERE name=?', (now, node))
        self._requeue_expired(c, now)
        live = self._live(c, now)
        if node not in live:
            return None
        caps = live[node][0]
        b = b or {}
        # Honor agent-reported free slots (accounts for processes still winding down after lease expiry)
        free = caps.get('slots', 1)
        if isinstance(b.get('free'), int) and not isinstance(b.get('free'), bool):
            free = min(free, b['free'])
        mem_free = b.get('mem') if isinstance(b.get('mem'), (int, float)) and not isinstance(b.get('mem'), bool) else None
        if free < 1:
            return None
        # Never assign a shard to a node already running or delivering it
        mine = {(e[0], e[1]) for e in b.get('running') or [] if isinstance(e, list) and len(e) >= 2}
        jobs, created, yields = [], {}, {}
        for j in c.execute("SELECT j.id, j.spec, COALESCE(j.activated, j.created) AS created, j.yield_until FROM jobs j WHERE EXISTS (SELECT 1 FROM shards s "
                           "WHERE s.job=j.id AND s.state IN ('queued','leased')) ORDER BY j.created").fetchall():
            jspec = json.loads(j['spec'])
            if self._eligible(jspec, node, caps):
                jobs.append((j['id'], jspec))
                created[j['id']], yields[j['id']] = j['created'], j['yield_until']
        # Sort whole-case jobs descending by est while keeping sharded jobs in FIFO order
        at = [i for i, (_, js) in enumerate(jobs) if js.get('kind') == 'case']
        for i, x in zip(at, sorted((jobs[i] for i in at), key=lambda x: (-(x[1].get('est') or 0), created[x[0]]))):
            jobs[i] = x
        newest = max((created[j] for j, js in jobs if js.get('kind') == 'case'), default=None)
        settling = newest is not None and now - newest < CASE_SETTLE_S
        plan, passed = None, []
        for job, jspec in jobs:
            case = jspec.get('kind') == 'case'
            if case and settling and now - created[job] < CASE_SETTLE_MAX_S:
                continue
            t = self._fit(jspec, free, mem_free)
            if t is None:
                continue
            s = next((r for r in c.execute("SELECT idx, spec, work FROM shards WHERE job=? AND state='queued' ORDER BY idx", (job,))
                      if (job, r['idx']) not in mine), None)
            if not s:
                continue
            if case and jspec.get('est') and not (yields[job] and now >= yields[job]):
                plan = plan or CasePlan(self, c, node, now, live)
                got = plan.offer(jspec, t)
                if got is not None:
                    ratio, start = got
                    passed.append((ratio, job, jspec, s, t))
                    if not yields[job]:
                        # Yield to faster peer with bounded deadline
                        until = now + start + max(CASE_YIELD_MIN_S, CASE_YIELD_SLACK * start)
                        c.execute('UPDATE jobs SET yield_until=? WHERE id=?', (until, job))
                        yields[job] = until
                    continue
            elif not case and not self._tail_ok(c, job, jspec, node, s['work'], live):
                continue
            why = 'claimed' if not (case and yields[job] and now >= yields[job]) else 'claimed (not taken where it was left)'
            return self._grant(c, node, job, jspec, s, now, why, t)
        if passed:
            ratio, job, jspec, s, t = min(passed, key=lambda x: x[0])
            if ratio <= CASE_IDLE_RATIO:
                return self._grant(c, node, job, jspec, s, now, f'claimed (finishes {ratio:.2f}x later than elsewhere)', t)
        # Queue is empty: speculatively duplicate shards held by suspended, offline, or much slower peers
        best = None
        for job, jspec in jobs:
            if self._fit(jspec, free, mem_free) is None:
                continue
            if c.execute("SELECT 1 FROM shards WHERE job=? AND state='queued'", (job,)).fetchone():
                continue
            r = self.rates(c, job)
            for s in c.execute("SELECT s.idx, s.spec, s.work, l.node AS holder, l.claimed AS lc, l.susp_from AS sf FROM shards s "
                               "JOIN leases l ON l.job=s.job AND l.idx=s.idx WHERE s.job=? AND s.state='leased' AND "
                               "(SELECT COUNT(*) FROM leases x WHERE x.job=s.job AND x.idx=s.idx)<? AND NOT EXISTS "
                               "(SELECT 1 FROM leases y WHERE y.job=s.job AND y.idx=s.idx AND y.node=?)",
                               (job, MAX_COPIES, node)).fetchall():
                if (job, s['idx']) in mine:
                    continue
                h = c.execute('SELECT state, last_seen, caps, susp_from FROM nodes WHERE name=?', (s['holder'],)).fetchone()
                hb = json.loads(h['caps'] or '{}').get('hb_s', HB_S) if h else HB_S
                sf = s['sf'] or (h['susp_from'] if h and h['state'] == 'suspend' else None)
                stuck = not h or not h['last_seen'] or now - h['last_seen'] > 2 * hb or bool(sf and now - sf >= SUSPEND_DUP_S)
                mine_t = s['work'] / r[node] if r.get(node) else None
                if stuck:
                    rem = float('inf')
                elif jspec.get('kind') != 'case' and r.get(s['holder']) and mine_t and not sf:
                    rem = s['work'] / r[s['holder']] - (now - s['lc'])
                    if rem <= DUP_GAIN * mine_t:
                        continue
                else:
                    continue
                if best is None or rem > best[0]:
                    best = (rem, job, jspec, s, sf)
        if best:
            _, job, jspec, s, sf = best
            t = self._fit(jspec, free, mem_free)
            why = f" (suspended {now - sf:.0f} s)" if sf else ''
            return self._grant(c, node, job, jspec, s, now, f"duplicate of {s['holder']}'s lease{why}", t)
        return None

    def progress(self, c, node, b, now):
        """Handle whole-case progress tick: renew lease, store progress & statepoints, or signal cancel."""
        job, idx, lid = b['job'], b['idx'], b.get('lease')
        c.execute('UPDATE nodes SET last_seen=MAX(last_seen, ?) WHERE name=?', (now, node))
        prog = b.get('progress') if isinstance(b.get('progress'), dict) else {}
        susp_s = prog.get('suspended_s')
        susp_s = float(susp_s) if isinstance(susp_s, (int, float)) and not isinstance(susp_s, bool) and susp_s >= 0 else None
        row = c.execute('SELECT progress, id FROM leases WHERE job=? AND idx=? AND node=?', (job, idx, node)).fetchone()
        if not row or (lid and row['id'] not in (None, lid)):
            # Lease revoked or superseded by newer execution: fence old worker
            return {'cancel': True}
        lid = lid or row['id']
        e = c.execute('SELECT ended FROM execs WHERE lease=? AND node=? AND job=? AND idx=?', (lid, node, job, idx)).fetchone()
        if e and e['ended'] is not None:
            return {'cancel': True}
        old = json.loads(row['progress'] or '{}')
        if old.get('wall_s') is not None and prog.get('wall_s') is not None and prog['wall_s'] < old['wall_s']:
            return {'cancel': False}
        for field in ('wall_s', 'suspended_s', 'batch', 'cycle'):
            if isinstance(old.get(field), (int, float)):
                prog[field] = max(old[field], prog.get(field) or 0)
        susp_s = prog.get('suspended_s', susp_s)
        c.execute('UPDATE execs SET seen=MAX(seen, ?), suspended_s=MAX(COALESCE(suspended_s, 0), COALESCE(?, 0)) '
                  'WHERE lease=? AND node=? AND job=? AND idx=? AND ended IS NULL', (now, susp_s, lid, node, job, idx))
        c.execute('UPDATE shards SET last_run=? WHERE job=? AND idx=?', (now, job, idx))
        c.execute('UPDATE leases SET progress=?, seen=?, until=?, susp_from=CASE WHEN ? THEN COALESCE(susp_from, ?) ELSE NULL END '
                  'WHERE job=? AND idx=? AND node=?', (json.dumps(prog), now, now + LEASE_S, bool(prog.get('suspended')), now, job, idx, node))
        sp = b.get('sp')
        if isinstance(sp, dict) and HEX64.match(str(sp.get('sha', ''))) and J.STATEPOINT.match(str(sp.get('name', ''))) \
                and isinstance(sp.get('batch'), int) and self.blob_path(sp['sha']).exists():
            r = c.execute('SELECT ckpt FROM shards WHERE job=? AND idx=?', (job, idx)).fetchone()
            old = json.loads(r['ckpt']) if r and r['ckpt'] else None
            if not old or sp['batch'] > old['batch']:
                c.execute('UPDATE shards SET ckpt=? WHERE job=? AND idx=?',
                          (json.dumps({'batch': sp['batch'], 'name': sp['name'], 'sha': sp['sha'], 'node': node, 't': now}), job, idx))
                self.event(c, node, job, idx, f"statepoint {sp['batch']}")
                if old and old['sha'] != sp['sha']:
                    self._drop_blob(c, old['sha'])
        return {'cancel': False}

    def _drop_blob(self, c, h):
        """Delete an unreferenced statepoint blob only if no shard or active lease references it."""
        pat = f'%{h}%'
        if c.execute('SELECT 1 FROM shards WHERE spec LIKE ? OR outputs LIKE ? OR ckpt LIKE ? LIMIT 1', (pat, pat, pat)).fetchone():
            return False
        if c.execute('SELECT 1 FROM leases WHERE inputs LIKE ? LIMIT 1', (pat,)).fetchone():
            return False
        try:
            self.blob_path(h).unlink()
        except FileNotFoundError:
            pass
        return True

    def _exec(self, c, node, job, idx, solver, ex_row=None):
        """Assemble execution provenance: node, agent/solver sha256, thread count, and restart batch."""
        n = c.execute('SELECT caps FROM nodes WHERE name=?', (node,)).fetchone()
        caps = json.loads(n['caps'] or '{}') if n else {}
        sv = (caps.get('solvers') or {}).get(solver) or {}
        l = c.execute('SELECT threads FROM leases WHERE job=? AND idx=? AND node=?', (job, idx, node)).fetchone()
        threads = ex_row['threads'] if ex_row else (l['threads'] if l else None)
        sh = c.execute('SELECT spec FROM shards WHERE job=? AND idx=?', (job, idx)).fetchone()
        out = {'node': node, 'host': caps.get('host'), 'agent': (caps.get('agent') or {}).get('sha'), 'exe': sv.get('exe'),
               'ident': sv.get('ident'), 'threads': threads}
        if ex_row and ex_row['start_batch']:
            out['restart_batch'] = ex_row['start_batch']
            out['spec_batch'] = json.loads(sh['spec']).get('restart_batch', 0) if sh else 0
        return out

    def done(self, c, node, b, now):
        job, idx, rc, lid = b['job'], b['idx'], b['rc'], b.get('lease')
        c.execute('UPDATE nodes SET last_seen=MAX(last_seen, ?) WHERE name=?', (now, node))
        n = c.execute('SELECT running FROM nodes WHERE name=?', (node,)).fetchone()
        if n and n['running']:
            c.execute('UPDATE nodes SET running=? WHERE name=?', (json.dumps([x for x in json.loads(n['running']) if x[:2] != [job, idx]]), node))
        susp = b.get('suspended_s')
        susp = float(susp) if isinstance(susp, (int, float)) and not isinstance(susp, bool) and susp >= 0 else 0.0
        # Always settle execution accounting (wall_s, suspended_s, rc) even if the shard already completed elsewhere
        ex_row = c.execute('SELECT * FROM execs WHERE lease=? AND node=? AND job=? AND idx=?', (lid, node, job, idx)).fetchone() if lid else \
            c.execute('SELECT * FROM execs WHERE job=? AND idx=? AND node=? ORDER BY claimed DESC LIMIT 1', (job, idx, node)).fetchone()
        if lid and not ex_row:
            return {'accepted': False, 'why': 'not lease holder'}
        if ex_row and ex_row['rc'] is None:
            c.execute('UPDATE execs SET ended=?, why=?, wall_s=?, suspended_s=?, rc=? WHERE lease=?',
                      (now, 'cancelled' if b.get('cancelled') else 'done', b.get('wall_s'), susp, rc, ex_row['lease']))
        r = c.execute('SELECT state, node, fails FROM shards WHERE job=? AND idx=?', (job, idx)).fetchone()
        if r is None:
            return {'accepted': False, 'why': 'unknown shard'}
        if r['state'] == 'done':
            self.event(c, node, job, idx, f"duplicate result ignored (accepted from {r['node']})")
            return {'accepted': False, 'why': 'already done'}
        if r['state'] in ('cancelled', 'failed'):
            self.event(c, node, job, idx, f"result for {r['state']} shard ignored rc={rc}")
            return {'accepted': False, 'why': r['state']}
        # Only attribute failures to the current lease holder; ignore stale or node-aborted runs
        hl = c.execute('SELECT id FROM leases WHERE job=? AND idx=? AND node=?', (job, idx, node)).fetchone()
        holds = bool(hl) and (not lid or hl['id'] in (None, lid))
        if rc != 0 and b.get('cancelled'):
            if holds:
                c.execute('DELETE FROM leases WHERE job=? AND idx=? AND node=?', (job, idx, node))
                self._unleased_to_queue(c)
            self.event(c, node, job, idx, f'ended by the node (cancelled) rc={rc}')
            return {'accepted': False, 'why': 'cancelled by node'}
        if rc != 0 and not holds:
            self.event(c, node, job, idx, f'failure from node without the lease ignored rc={rc}')
            return {'accepted': False, 'why': 'not lease holder'}
        for h in b['outputs'].values():
            if not HEX64.match(h) or not self.blob_path(h).exists():
                return {'accepted': False, 'why': f'missing blob {h}'}
        j = c.execute('SELECT solver, kind FROM jobs WHERE id=?', (job,)).fetchone()
        case = j['kind'] == 'case'
        ex = self._exec(c, node, job, idx, j['solver'], ex_row)
        if isinstance(b.get('threads'), int) and not isinstance(b.get('threads'), bool):
            ex['threads'] = b['threads']
        if rc == 0:
            # Accept valid rc=0 results even after lease expiry since shards are deterministic
            c.execute("UPDATE shards SET state='done', node=?, outputs=?, cpu_s=?, wall_s=?, finished=?, rc=0, exec=?, suspended_s=? "
                      "WHERE job=? AND idx=?",
                      (node, json.dumps(b['outputs']), b['cpu_s'], b['wall_s'], now, json.dumps(ex), susp, job, idx))
            c.execute('DELETE FROM leases WHERE job=? AND idx=?', (job, idx))
            self.event(c, node, job, idx, f"done rc=0 cpu={b['cpu_s']:.1f}s wall={b['wall_s']:.1f}s")
            return {'accepted': True}
        c.execute('DELETE FROM leases WHERE job=? AND idx=? AND node=?', (job, idx, node))
        fails = r['fails'] + 1
        left = c.execute('SELECT COUNT(*) FROM leases WHERE job=? AND idx=?', (job, idx)).fetchone()[0]
        state = 'leased' if left else ('failed' if fails >= (CASE_FAIL_LIMIT if case else FAIL_LIMIT) else 'queued')
        c.execute('UPDATE shards SET state=?, node=NULL, fails=?, rc=?, cpu_s=?, wall_s=?, finished=?, suspended_s=? WHERE job=? AND idx=?',
                  (state, fails, rc, b['cpu_s'], b['wall_s'], now, susp, job, idx))
        if case and state == 'failed':
            c.execute('UPDATE shards SET node=?, outputs=?, exec=? WHERE job=? AND idx=?',
                      (node, json.dumps(b['outputs']), json.dumps(ex), job, idx))
        self.event(c, node, job, idx, f"done rc={rc} cpu={b['cpu_s']:.1f}s wall={b['wall_s']:.1f}s -> {state}")
        return {'accepted': False, 'why': f'rc={rc}'}

    # ---------- Admin API ----------
    def node_list(self, c, now):
        rows = {r['name']: r for r in c.execute('SELECT * FROM nodes').fetchall()}
        used = {r[0]: r[1] for r in c.execute('SELECT node, COUNT(*) FROM leases GROUP BY node')}
        slots_used = {r[0]: r[1] for r in c.execute('SELECT node, SUM(threads) FROM leases GROUP BY node')}
        solver_use = {(r[0], r[1]): (r[2], r[3]) for r in c.execute(
            'SELECT l.node, j.solver, COUNT(*), SUM(l.threads) FROM leases l '
            'JOIN jobs j ON j.id=l.job GROUP BY l.node, j.solver')}
        out = []
        for name in sorted(set(rows) | set(self.tokens())):
            r = rows.get(name)
            caps = json.loads(r['caps'] or '{}') if r else {}
            res = json.loads(r['res'] or '{}') if r and r['res'] else {}
            hb = caps.get('hb_s', HB_S)
            age = now - r['last_seen'] if r and r['last_seen'] else None
            online = age is not None and age <= 2 * hb and not (r and r['reject'])
            enabled = bool(r['enabled']) if r else True
            state = (r['state'] or 'active') if r else 'active'
            n_used = used.get(name, 0)
            hold_s = max(0.0, r['hold_until'] - now) if r and r['hold_until'] else 0.0
            if not online:
                status = 'offline'
            elif not enabled:
                status = 'disabled'
            elif state == 'suspend':
                status = 'suspend'
            elif state == 'hold' or hold_s > 0:
                status = 'hold'
            else:
                status = 'run' if n_used else 'idle'
            # Expose capability summaries only; jobs verify per-file digests during dispatch
            solvers = {}
            for solver in ('openmc', 'ref_mc'):
                cap = (caps.get('solvers') or {}).get(solver) or {}
                tasks, occupied = solver_use.get((name, solver), (0, 0))
                solvers[solver] = {'registered': bool(cap.get('exe')), 'exe': cap.get('exe'),
                                   'data_files': len(cap.get('files') or {}), 'tasks': tasks, 'slots_used': occupied}
            out.append({'name': name, 'online': online, 'status': status, 'enabled': enabled, 'state': state,
                        'solvers': solvers,
                        'used': n_used, 'slots_used': slots_used.get(name, 0),
                        'running': len(json.loads(r['running'])) if r and r['running'] and online else 0,
                        'hold_s': round(hold_s, 1), 'hold_why': r['hold_why'] if hold_s > 0 else None, 'hostname': caps.get('host'),
                        'slots': caps.get('slots'), 'cpus': res.get('cpus'), 'ncpu': res.get('ncpu', caps.get('cpu_count')),
                        'mem_bytes': res.get('mem_bytes'), 'load1': res.get('load1'), 'rate': res.get('rate'),
                        'host': res.get('host'), 'hb_s': hb, 'seen_s': age,
                        'proto': r['proto'] if r else None, 'agent': caps.get('agent', {}).get('sha'),
                        'reject': json.loads(r['reject'])['why'] if r and r['reject'] else None})
        return {'nodes': out, 'proto': PROTO, 'lease_s': LEASE_S}

    def _node_row(self, c, name):
        if name not in self.tokens() and not c.execute('SELECT 1 FROM nodes WHERE name=?', (name,)).fetchone():
            raise HttpError(404, f'Unknown node {name}')
        c.execute('INSERT OR IGNORE INTO nodes(name, caps) VALUES(?, ?)', (name, '{}'))

    def set_enabled(self, c, name, enabled):
        self._node_row(c, name)
        c.execute('UPDATE nodes SET enabled=? WHERE name=?', (1 if enabled else 0, name))
        self.event(c, name, None, None, 'enabled by admin' if enabled else 'disabled by admin')
        return {'name': name, 'enabled': bool(enabled)}

    def stop_now(self, c, name):
        """Disable node and revoke its leases; agents terminate revoked runs on next heartbeat."""
        self._node_row(c, name)
        c.execute('UPDATE nodes SET enabled=0 WHERE name=?', (name,))
        n = self._release(c, name, 'stopped by admin, requeued')
        self.event(c, name, None, None, f'stopped by admin, {n} leases released')
        return {'name': name, 'enabled': False, 'released': n}

    def request_rescan(self, c, name):
        self._node_row(c, name)
        c.execute('UPDATE nodes SET rescan=1 WHERE name=?', (name,))
        return {'name': name, 'rescan': True}

    def set_hold(self, c, name, seconds, why):
        """Temporarily pause claims on node for seconds (0 clears); expires automatically on crash."""
        self._node_row(c, name)
        until = time.time() + seconds if seconds > 0 else None
        c.execute('UPDATE nodes SET hold_until=?, hold_why=? WHERE name=?', (until, why if until else None, name))
        return {'name': name, 'hold_s': seconds}

    def _probe(self):
        p = self.var / 'probe.json'
        if not p.exists():
            raise HttpError(503, f'Missing {p}; cannot create jobs')
        return json.loads(p.read_text())['solvers']

    @staticmethod
    def _int(b, k, lo, hi, default=None):
        v = b.get(k, default)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise HttpError(400, f'{k} must be an integer between {lo} and {hi}')
        return v

    def create_job(self, b):
        jid = b.get('id')
        if not isinstance(jid, str) or not JOB_ID.match(jid):
            raise HttpError(400, 'id must be 1-64 alphanumeric, dot, underscore, or hyphen characters')
        nodes = b.get('nodes')
        if nodes is not None and (not isinstance(nodes, list) or not all(isinstance(x, str) for x in nodes) or not nodes):
            raise HttpError(400, 'nodes must be a non-empty list of node names')
        if self.read(lambda c: c.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone()):
            raise HttpError(409, f'Job {jid} already exists')
        solvers = self._probe()
        solver = b.get('solver')
        kind = b.get('kind', 'shards')
        if kind == 'case':
            spec, shard = self._case_spec(b, solver, solvers)
            if nodes:
                spec['nodes'] = nodes
            if b.get('dry_run'):
                ok = self.read(lambda c: self._ready(c, spec, time.time()))
                return {'id': jid, 'kind': 'case', 'dry_run': True, 'requires': spec['requires'], 'nodes_ready': ok}
            if b.get('hold') is not None and not isinstance(b.get('hold'), bool):
                raise HttpError(400, 'hold must be boolean')
            return self.tx(self._insert_case, jid, spec, shard, bool(b.get('hold')))
        if kind != 'shards':
            raise HttpError(400, 'kind must be shards or case')
        if solver == 'ref_mc':
            deck = b.get('deck')
            if not isinstance(deck, str) or not deck.strip():
                raise HttpError(400, 'deck must be full Reference MC input text')
            total = self._int(b, 'histories', 1, J.HIST_LIMIT)
            if total is None:
                raise HttpError(400, 'Missing histories')
            s = solvers['ref_mc']
            base = J.ref_mc_base(deck)
            try:
                files = self.probe_ref_mc(base, s['data_root'], s['exe'])
            except Exception as e:
                raise HttpError(400, f'Probe failed: {e}')
            spec = {'solver': 'ref_mc', 'args': J.REF_MC_ARGS, 'outputs': J.REF_MC_OUTPUTS,
                    'requires': {'exe': J.ident(s['identity']), 'files': files}}
        elif solver == 'openmc':
            model = b.get('model')
            if not isinstance(model, str) or '<settings' not in model:
                raise HttpError(400, 'model must be full OpenMC single-file model.xml')
            particles = self._int(b, 'particles', 1, 10 ** 9)
            batches = self._int(b, 'batches', 2, 10 ** 5)
            if particles is None or batches is None:
                raise HttpError(400, 'Missing particles or batches')
            threads = self._int(b, 'threads', 1, 256, 1)
            s = solvers['openmc']
            try:
                files = J.openmc_files(model, s['data_root'])
            except (ValueError, OSError, KeyError) as e:
                raise HttpError(400, str(e))
            except Exception as e:
                raise HttpError(400, f'Cannot parse model.xml: {e}')
            spec = {'solver': 'openmc', 'args': ['-s', str(threads), '.'], 'outputs': J.OPENMC_OUTPUTS, 'threads': threads,
                    'requires': {'exe': J.ident(s['identity']), 'files': files}}
        else:
            raise HttpError(400, 'solver must be ref_mc or openmc')
        if nodes:
            spec['nodes'] = nodes
        return self.tx(self._insert_job, jid, spec, b)

    def _case_spec(self, b, solver, solvers):
        """Build spec & shard for an unsharded whole-case job."""
        for k in b:
            if k not in ('id', 'kind', 'solver', 'model', 'deck', 'threads', 'min_threads', 'mem_mb', 'restart', 'nodes', 'ckpt_s', 'dry_run',
                         'est', 'hold', 'requires'):
                raise HttpError(400, f'Unexpected parameter for whole-case job: {k}')
        mem_mb = self._int(b, 'mem_mb', 1, 1 << 20)
        est = b.get('est')
        if est is not None and (isinstance(est, bool) or not isinstance(est, (int, float)) or not 0 < est <= 1e9):
            raise HttpError(400, 'est must be a positive number (baseline core-seconds)')
        req = b.get('requires')
        if req is not None:
            if not isinstance(req, dict) or not HEX64.match(str(req.get('exe', ''))) or not isinstance(req.get('files', {}), dict) \
                    or not all(isinstance(k, str) and HEX64.match(str(v)) for k, v in req.get('files', {}).items()):
                raise HttpError(400, 'requires must be {exe: sha256, files: {path: sha256}}')
        if solver == 'openmc':
            model = b.get('model')
            if not isinstance(model, str) or '<settings' not in model:
                raise HttpError(400, 'model must be full OpenMC single-file model.xml')
            hi = self._int(b, 'threads', 1, 256, 4)
            lo = self._int(b, 'min_threads', 1, hi, 1)
            s = solvers['openmc']
            try:
                files = J.openmc_files(model, s['data_root'])
                work = J.openmc_work(model)
            except (ValueError, OSError, KeyError) as e:
                raise HttpError(400, str(e))
            except Exception as e:
                raise HttpError(400, f'Cannot parse model.xml: {e}')
            mem = mem_mb * 1048576 if mem_mb else self._mem_default('openmc', s['data_root'], files)
            if req is not None and 'files' in req and set(req['files']) != set(files):
                raise HttpError(400, 'Requested data files do not match model requirements: ' + ', '.join(sorted(set(req['files']) ^ set(files))))
            spec = {'solver': 'openmc', 'kind': 'case', 'args': J.CASE_OPENMC_ARGS, 'outputs': J.CASE_OPENMC_OUTPUTS, 'log': 'run.log',
                    'threads': [lo, hi], 'mem': mem, 'ckpt_s': self._int(b, 'ckpt_s', 10, 86400, CKPT_S),
                    'requires': {'exe': req['exe'] if req else J.ident(s['identity']),
                                 'files': req['files'] if req and 'files' in req else files},
                    'steps': J.openmc_batches(model)}
            shard = {'inputs': {'model.xml': self.put_blob(model.encode())}, 'work': work}
            r = b.get('restart')
            if r is not None:
                if not isinstance(r, dict) or not J.STATEPOINT.match(str(r.get('name', ''))) or not HEX64.match(str(r.get('sha', ''))):
                    raise HttpError(400, 'restart must be {name: statepoint.N.h5, sha: sha256}')
                if not self.blob_path(r['sha']).exists():
                    raise HttpError(409, f"Coordinator missing statepoint blob {r['sha']}; upload first")
                shard['inputs'][r['name']] = r['sha']
                shard['restart'] = r['name']
                shard['restart_batch'] = int(r['name'].split('.')[1])
        elif solver == 'ref_mc':
            deck = b.get('deck')
            if not isinstance(deck, str) or not deck.strip():
                raise HttpError(400, 'deck must be full Reference MC input text')
            kc = J.ref_mc_kcode(deck)
            if not kc:
                raise HttpError(400, 'Whole-case Reference MC input must include a KCODE card')
            s = solvers['ref_mc']
            base = J.ref_mc_case_probe_base(deck)
            key = hashlib.sha256('\n'.join(base).encode('latin-1', 'replace')).hexdigest()
            files = self._probes.get(key)
            if files is None:
                try:
                    files = self.probe_ref_mc(base, s['data_root'], s['exe'])
                except Exception as e:
                    raise HttpError(400, f'Probe failed: {e}')
                self._probes[key] = files
            mem = mem_mb * 1048576 if mem_mb else self._mem_default('ref_mc', s['data_root'], files)
            spec = {'solver': 'ref_mc', 'kind': 'case', 'args': J.CASE_REF_MC_ARGS, 'outputs': J.CASE_REF_MC_OUTPUTS, 'log': 'run.log',
                    'env': J.CASE_REF_MC_ENV, 'threads': [1, 1], 'mem': mem,
                    'requires': {'exe': req['exe'] if req else J.ident(s['identity']), 'files': files}, 'steps': kc[1]}
            shard = {'inputs': {'case.i': self.put_blob(deck.encode('latin-1'))}, 'work': kc[0] * kc[1]}
        else:
            raise HttpError(400, 'solver must be ref_mc or openmc')
        if est is not None:
            spec['est'] = float(est)
        return spec, shard

    @staticmethod
    def _mem_default(solver, data_root, files):
        """Estimate solver RSS from nuclear data footprint + base overhead."""
        size = J.files_size(data_root, files)
        return int(0.6 * size + 150 * 1048576) if solver == 'openmc' else int(0.4 * size + 100 * 1048576)

    def _plan_spec(self, j):
        """Normalize a hypothetical /admin/plan entry into a whole-case spec."""
        if not isinstance(j, dict):
            raise HttpError(400, 'Each job entry must be an object')
        for k in j:
            if k not in ('est', 'threads', 'mem_mb', 'solver', 'requires', 'nodes'):
                raise HttpError(400, f'Unexpected parameter in planned job: {k}')
        solver = j.get('solver', 'openmc')
        if solver not in ('openmc', 'ref_mc'):
            raise HttpError(400, 'solver must be ref_mc or openmc')
        est = j.get('est')
        if isinstance(est, bool) or not isinstance(est, (int, float)) or not 0 < est <= 1e9:
            raise HttpError(400, 'est must be a positive number (baseline core-seconds)')
        th = j.get('threads', [1, 4] if solver == 'openmc' else [1, 1])
        if not (isinstance(th, list) and len(th) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in th)
                and 1 <= th[0] <= th[1] <= 256):
            raise HttpError(400, 'threads must be [min_threads, max_threads] in 1..256')
        if solver == 'ref_mc':
            th = [1, 1]
        req = j.get('requires')
        if req is not None:
            if not isinstance(req, dict) or not HEX64.match(str(req.get('exe', ''))) or not isinstance(req.get('files', {}), dict) \
                    or not all(isinstance(k, str) and HEX64.match(str(v)) for k, v in req.get('files', {}).items()):
                raise HttpError(400, 'requires must be {exe: sha256, files: {path: sha256}}')
            req = {'exe': req['exe'], 'files': dict(req.get('files') or {})}
        mem_mb = self._int(j, 'mem_mb', 1, 1 << 20)
        mem = mem_mb * 1048576 if mem_mb else None
        if req is None or (mem is None and req['files']):
            solvers = self._probe()
            if req is None:
                req = {'exe': J.ident(solvers[solver]['identity']), 'files': {}}
            if mem is None and req['files']:
                try:
                    mem = self._mem_default(solver, solvers[solver]['data_root'], req['files'])
                except OSError:
                    mem = None
        spec = {'kind': 'case', 'solver': solver, 'est': float(est), 'threads': th, 'mem': mem, 'requires': req}
        nodes = j.get('nodes')
        if nodes is not None:
            if not isinstance(nodes, list) or not nodes or not all(isinstance(x, str) for x in nodes):
                raise HttpError(400, 'nodes must be a non-empty list of node names')
            spec['nodes'] = nodes
        return spec

    def plan(self, c, b, now):
        """Read-only makespan simulation combining queued jobs and hypothetical jobs via CasePlan."""
        jobs = b.get('jobs')
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= PLAN_MAX:
            raise HttpError(400, f'jobs must be a list of 1 to {PLAN_MAX} entries')
        specs = [self._plan_spec(j) for j in jobs]
        live = self._live(c, now)
        cp = CasePlan(self, c, None, now, live)
        queued = [js for js in (json.loads(r['spec']) for r in c.execute(
            "SELECT j.spec FROM jobs j JOIN shards s ON s.job=j.id AND s.idx=0 WHERE j.kind='case' AND s.state='queued' "
            "ORDER BY j.created")) if js.get('est')]
        out = [None] * len(specs)
        for js, i in sorted([(js, None) for js in queued] + [(js, i) for i, js in enumerate(specs)], key=lambda x: -x[0]['est']):
            best = cp.best(js)
            if best is None:
                continue
            cp.take(js, best)
            if i is not None:
                f, y, ty, start = best
                out[i] = {'node': y, 'threads': ty, 'start_s': round(start, 1), 'finish_s': round(f, 1)}
        done = [p['finish_s'] for p in out if p]
        return {'finish_s': max(done) if done else None, 'jobs': out, 'nodes': sorted({p['node'] for p in out if p}),
                'queued': len(queued), 'live': sorted(live)}

    def _ready(self, c, spec, now):
        """Nodes online, enabled, active, unheld, matching binaries/data, with sufficient slots and RAM."""
        live = self._live(c, now)
        need = spec.get('mem')
        return sorted(n for n, (caps, row) in live.items() if self._eligible(spec, n, caps)
                      and caps.get('slots', 1) >= spec['threads'][0]
                      and (_partial_free(row) is None or _partial_free(row) >= spec['threads'][0])
                      and not (need and _mem_total(row) is not None and need > _mem_total(row)))

    def _insert_case(self, c, jid, spec, shard, held=False):
        """Insert whole-case job (optionally held pending two-phase activate())."""
        if c.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
            raise HttpError(409, f'Job {jid} already exists')
        now = time.time()
        ok = self._ready(c, spec, now)
        spec['plan'] = {'kind': 'case', 'threads': spec['threads'], 'work': shard['work']}
        if spec.get('steps'):
            spec['plan']['steps'] = spec.pop('steps')
        c.execute('INSERT INTO jobs(id, solver, spec, created, kind) VALUES(?,?,?,?,?)', (jid, spec['solver'], json.dumps(spec), now, 'case'))
        c.execute('INSERT INTO shards(job, idx, spec, state, work) VALUES(?,0,?,?,?)',
                  (jid, json.dumps(shard), 'held' if held else 'queued', shard['work']))
        self.event(c, None, jid, None, f"created case job threads={spec['threads']}" + (' (held)' if held else ''))
        return {'id': jid, 'solver': spec['solver'], 'kind': 'case', 'shards': 1, 'plan': spec['plan'],
                'files': len(spec['requires']['files']), 'requires': spec['requires'], 'nodes_ready': ok}

    def _insert_job(self, c, jid, spec, b):
        if c.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
            raise HttpError(409, f'Job {jid} already exists')
        now = time.time()
        live = self._live(c, now)
        ok = [n for n, (caps, _) in live.items() if self._eligible(spec, n, caps)]
        slots = sum(live[n][0].get('slots', 1) for n in ok)
        # Create at least 4x online slots so proportional pulling keeps tail wait around one shard
        k = self._int(b, 'shards', 1, 100000) or max(4, 4 * slots)
        shards = []
        if spec['solver'] == 'ref_mc':
            total = b['histories']
            k = min(k, total)
            start = self._int(b, 'hist0', 1, J.HIST_LIMIT)
            if start is None:
                start = c.execute('SELECT COALESCE(MAX(start + count), 1) FROM ranges').fetchone()[0]
            end = start + total
            if end - 1 > J.HIST_LIMIT:
                raise HttpError(409, f'History range exceeds GEN=2 PRNG limit ({J.HIST_LIMIT})')
            hit = c.execute('SELECT job, start, count FROM ranges WHERE start < ? AND start + count > ?', (end, start)).fetchone()
            if hit:
                raise HttpError(409, f"History range {start}-{end - 1} overlaps job {hit['job']} ({hit['start']}-{hit['start'] + hit['count'] - 1})")
            c.execute('INSERT INTO ranges VALUES(?,?,?)', (jid, start, total))
            base = J.ref_mc_base(b['deck'])
            h = start
            for n in J.split_counts(total, k):
                inp = self.put_blob(J.ref_mc_shard_text(base, n, h).encode('latin-1'))
                shards.append({'inputs': {'inp': inp}, 'hist': h, 'nps': n, 'work': n})
                h += n
            spec['plan'] = {'gen': 2, 'histories': total, 'hist0': start, 'shards': k}
        else:
            for i in range(k):
                seed = J.openmc_seed(jid, i)
                x = self.put_blob(J.openmc_shard_xml(b['model'], b['particles'], b['batches'], seed))
                shards.append({'inputs': {'model.xml': x}, 'seed': seed, 'work': b['particles'] * b['batches']})
            spec['plan'] = {'particles': b['particles'], 'batches': b['batches'], 'shards': k}
        c.execute('INSERT INTO jobs(id, solver, spec, created, kind) VALUES(?,?,?,?,?)', (jid, spec['solver'], json.dumps(spec), now, 'shards'))
        for i, s in enumerate(shards):
            c.execute("INSERT INTO shards(job, idx, spec, state, work) VALUES(?,?,?,'queued',?)", (jid, i, json.dumps(s), s['work']))
        self.event(c, None, jid, None, f'created {k} shards')
        return {'id': jid, 'solver': spec['solver'], 'shards': k, 'plan': spec['plan'], 'files': len(spec['requires']['files']),
                'nodes_ready': sorted(ok)}

    @staticmethod
    def _job_state(by):
        if by.get('cancelled'):
            return 'cancelled'
        if by.get('held'):
            return 'held'
        if by.get('queued') or by.get('leased'):
            return 'running' if by.get('leased') or by.get('done') else 'queued'
        return 'failed' if by.get('failed') else 'done'

    def job_list(self, c):
        out = []
        for j in c.execute('SELECT id, solver, created FROM jobs ORDER BY created DESC').fetchall():
            by = {r[0]: r[1] for r in c.execute('SELECT state, COUNT(*) FROM shards WHERE job=? GROUP BY state', (j['id'],))}
            w = c.execute("SELECT SUM(work), SUM(CASE WHEN state='done' THEN work ELSE 0 END) FROM shards WHERE job=?", (j['id'],)).fetchone()
            out.append({'id': j['id'], 'solver': j['solver'], 'created': j['created'], 'state': self._job_state(by),
                        'shards': sum(by.values()), 'by_state': by, 'work': w[0], 'work_done': w[1]})
        return {'jobs': out}

    def job_view(self, c, jid, events=False):
        self._requeue_expired(c, time.time())
        j = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j:
            raise HttpError(404, f'Unknown job {jid}')
        spec = json.loads(j['spec'])
        rows = c.execute('SELECT idx, state, node, work, attempts, fails, cpu_s, wall_s, claimed, finished FROM shards '
                         'WHERE job=? ORDER BY idx', (jid,)).fetchall()
        by, per = {}, {}
        total = sum(r['work'] for r in rows)
        for r in rows:
            by[r['state']] = by.get(r['state'], 0) + 1
            if r['state'] == 'done':
                p = per.setdefault(r['node'], {'shards': 0, 'work': 0.0, 'cpu_s': 0.0, 'wall_s': 0.0})
                p['shards'] += 1
                p['work'] += r['work']
                p['cpu_s'] += r['cpu_s'] or 0.0
                p['wall_s'] += r['wall_s'] or 0.0
        rates = self.rates(c, jid)
        for n, p in per.items():
            p['share'] = p['work'] / total if total else 0.0
            p['rate_per_slot'] = rates.get(n)
        leases = [dict(r) for r in c.execute('SELECT idx, node, until, claimed FROM leases WHERE job=? ORDER BY idx', (jid,))]
        t = c.execute('SELECT MIN(claimed), MAX(finished) FROM shards WHERE job=?', (jid,)).fetchone()
        out = {'id': jid, 'solver': j['solver'], 'created': j['created'], 'state': self._job_state(by), 'plan': spec.get('plan'),
               'nodes': spec.get('nodes'), 'shards': len(rows), 'by_state': by, 'work': total, 'per_node': per,
               'leases': leases, 'span_s': (t[1] - t[0]) if t[0] and t[1] else None,
               'max_attempts': max((r['attempts'] for r in rows), default=0)}
        if events:
            out['events'] = [dict(e) for e in c.execute('SELECT * FROM events WHERE job=? ORDER BY t', (jid,))]
        if j['kind'] == 'case':
            out['case'] = self.case_view(c, jid, spec, time.time())
        return out

    def case_view(self, c, jid, spec, now):
        """Detailed whole-case status including active leases, execution attempts, and live node set."""
        s = c.execute('SELECT * FROM shards WHERE job=? AND idx=0', (jid,)).fetchone()
        leases = []
        for l in c.execute('SELECT node, threads, claimed, until, progress, seen, susp_from FROM leases WHERE job=? AND idx=0 '
                           'ORDER BY claimed', (jid,)):
            leases.append({'node': l['node'], 'threads': l['threads'], 'claimed': l['claimed'], 'until': l['until'],
                           'seen': l['seen'], 'susp_from': l['susp_from'], 'progress': json.loads(l['progress']) if l['progress'] else None})
        execs, running = [], set()
        for e in c.execute('SELECT e.*, n.last_seen AS nseen, n.caps AS ncaps FROM execs e LEFT JOIN nodes n ON n.name=e.node '
                           'WHERE e.job=? AND e.idx=0 ORDER BY e.claimed', (jid,)).fetchall():
            hb = json.loads(e['ncaps'] or '{}').get('hb_s', HB_S) if e['ncaps'] else HB_S
            contact = max(x for x in (e['seen'], e['nseen'], e['claimed']) if x is not None)
            live = e['ended'] is None and now - contact <= RENEW_LIMIT_S + 2 * hb
            if live:
                running.add(e['node'])
            execs.append({'id': e['lease'], 'node': e['node'], 'threads': e['threads'], 'claimed': e['claimed'], 'seen': e['seen'], 'ended': e['ended'], 'contact': contact,
                          'why': e['why'], 'wall_s': e['wall_s'], 'suspended_s': e['suspended_s'], 'rc': e['rc'],
                          'start_batch': e['start_batch'], 'live': live})
        # Legacy fallback when no exec rows exist
        for n in c.execute('SELECT name, last_seen, running FROM nodes WHERE running IS NOT NULL').fetchall():
            if n['last_seen'] and now - n['last_seen'] <= LEASE_S and [jid, 0] in json.loads(n['running']) and not execs:
                running.add(n['name'])
        return {'state': s['state'], 'leases': leases, 'execs': execs, 'running': sorted(running), 'claimed': s['claimed'],
                'last_run': s['last_run'],
                'finished': s['finished'], 'rc': s['rc'], 'wall_s': s['wall_s'], 'suspended_s': s['suspended_s'], 'cpu_s': s['cpu_s'],
                'attempts': s['attempts'],
                'outputs': json.loads(s['outputs']) if s['outputs'] else None, 'exec': json.loads(s['exec']) if s['exec'] else None,
                'ckpt': json.loads(s['ckpt']) if s['ckpt'] else None, 'requires': spec['requires'], 'threads': spec.get('threads'),
                'shard': json.loads(s['spec'])}

    def withdraw(self, c, jid):
        """Withdraw an unclaimed whole-case job (queued or held); leaves already-leased runs untouched."""
        j = c.execute('SELECT kind FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j:
            raise HttpError(404, f'Unknown job {jid}')
        s = c.execute('SELECT state FROM shards WHERE job=? AND idx=0', (jid,)).fetchone()
        if j['kind'] != 'case' or s['state'] not in ('queued', 'held') or c.execute('SELECT 1 FROM leases WHERE job=?', (jid,)).fetchone():
            return {'id': jid, 'withdrawn': False, 'state': s['state'] if s else None}
        c.execute("UPDATE shards SET state='cancelled' WHERE job=? AND idx=0", (jid,))
        self.event(c, None, jid, None, 'withdrawn while no node had it')
        return {'id': jid, 'withdrawn': True, 'state': 'cancelled'}

    def activate(self, c, jid):
        """Transition a held whole-case job to queued (two-phase commit step 2)."""
        self._requeue_expired(c, time.time())
        j = c.execute('SELECT kind FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j:
            raise HttpError(404, f'Unknown job {jid}')
        s = c.execute('SELECT state FROM shards WHERE job=? AND idx=0', (jid,)).fetchone()
        if s['state'] != 'held':
            if s['state'] in ('queued', 'leased', 'done'):
                return {'id': jid, 'activated': True, 'state': s['state']}
            raise HttpError(409, f"Job {jid} is already {s['state']} and cannot be activated", commit=True)
        c.execute("UPDATE shards SET state='queued' WHERE job=? AND idx=0", (jid,))
        c.execute('UPDATE jobs SET activated=? WHERE id=?', (time.time(), jid))
        self.event(c, None, jid, None, 'activated')
        return {'id': jid, 'activated': True, 'state': 'queued'}

    def purge(self, c, jid):
        """Delete outputs, checkpoint, and restart statepoints for a finished whole-case job if unreferenced."""
        j = c.execute('SELECT kind FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j:
            raise HttpError(404, f'Unknown job {jid}')
        if j['kind'] != 'case':
            raise HttpError(409, 'Only whole-case jobs can be purged')
        s = c.execute('SELECT state, outputs, ckpt, spec FROM shards WHERE job=? AND idx=0', (jid,)).fetchone()
        if s['state'] in ('queued', 'leased', 'held'):
            raise HttpError(409, f'Job {jid} is still active')
        hs = list((json.loads(s['outputs']) or {}).values()) if s['outputs'] else []
        if s['ckpt']:
            hs.append(json.loads(s['ckpt'])['sha'])
        sh = json.loads(s['spec'])
        if sh.get('restart') and sh.get('inputs', {}).get(sh['restart']):
            hs.append(sh['inputs'].pop(sh['restart']))
        for e in c.execute('SELECT inputs FROM execs WHERE job=?', (jid,)).fetchall():
            hs += [h for n, h in json.loads(e['inputs'] or '{}').items() if J.STATEPOINT.match(n)]
        c.execute('UPDATE shards SET outputs=NULL, ckpt=NULL, spec=? WHERE job=? AND idx=0', (json.dumps(sh), jid))
        n = sum(1 for h in set(hs) if self._drop_blob(c, h))
        self.event(c, None, jid, None, f'purged {n} files')
        return {'id': jid, 'purged': n}

    def cancel_job(self, c, jid):
        if not c.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
            raise HttpError(404, f'Unknown job {jid}')
        n = c.execute("UPDATE shards SET state='cancelled' WHERE job=? AND state IN ('queued','leased','held')", (jid,)).rowcount
        c.execute('DELETE FROM leases WHERE job=?', (jid,))
        self.event(c, None, jid, None, f'cancelled {n} shards')
        return {'id': jid, 'cancelled': n}

    def merge(self, jid):
        def rows(c):
            j = c.execute('SELECT spec FROM jobs WHERE id=?', (jid,)).fetchone()
            if not j:
                raise HttpError(404, f'Unknown job {jid}')
            done = c.execute("SELECT idx, node, outputs FROM shards WHERE job=? AND state='done' ORDER BY idx", (jid,)).fetchall()
            total = c.execute('SELECT COUNT(*) FROM shards WHERE job=?', (jid,)).fetchone()[0]
            return json.loads(j['spec']), done, total
        spec, done, total = self.read(rows)
        # Merge only a contiguous prefix from shard 0 so results are order-invariant
        prefix = []
        for i, r in enumerate(done):
            if r['idx'] != i:
                break
            prefix.append(r)
        if not prefix:
            raise HttpError(409, 'No contiguous prefix starting at shard 0 is complete yet')
        out = {'id': jid, 'solver': spec['solver'], 'shards_used': len(prefix), 'shards': total}
        if spec['solver'] == 'ref_mc':
            parts = [J.read_mctal(self.blob_path(json.loads(r['outputs'])['mctl']).read_text(encoding='latin-1')) for r in prefix]
            N, res, chi = J.merge_mctal(parts)
            out.update(nps=N, tallies={str(t): v for t, v in res.items()}, chi2={str(t): v for t, v in chi.items()})
        else:
            try:
                out['tallies'] = J.merge_openmc([self.blob_path(json.loads(r['outputs'])['statepoint.*.h5']) for r in prefix])
            except ImportError:
                raise HttpError(501, 'Coordinator Python environment lacks openmc; cannot merge OpenMC tallies')
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    coord = None

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj=None, body=None, ctype='application/json'):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get('Content-Length', 0))
        try:
            b = json.loads(self.rfile.read(n) or b'{}')
        except json.JSONDecodeError:
            raise HttpError(400, 'Request body is not valid JSON')
        if not isinstance(b, dict):
            raise HttpError(400, 'Request body must be a JSON object')
        return b

    def _node(self):
        node = self.headers.get('X-Node', '')
        tok = self.tokens_for(node)
        if not tok or not hmac.compare_digest(tok, self.headers.get('X-Token', '')):
            raise HttpError(403, 'bad token')
        proto = self.headers.get('X-Proto')
        try:
            proto = int(proto) if proto is not None else None
        except ValueError:
            proto = None
        if proto != PROTO:
            why = self.coord.tx(self.coord.reject, node, proto, self.client_address[0], time.time())
            raise HttpError(426, why)
        self.coord.tx(lambda c: c.execute('UPDATE nodes SET last_seen=MAX(last_seen, ?) WHERE name=?', (time.time(), node)))
        return node

    def tokens_for(self, node):
        return self.coord.tokens().get(node) if node else None

    def _admin(self):
        a = self.headers.get('Authorization', '')
        if not a.startswith('Bearer ') or not hmac.compare_digest(a[7:].strip(), self.coord.admin_token()):
            raise HttpError(403, 'Invalid admin token')

    def _handle(self, fn):
        try:
            fn()
        except HttpError as e:
            # Close connection on error so unread request body bytes cannot corrupt pipelined requests
            self.close_connection = True
            self._send(e.code, {'error': e.msg})
        except (KeyError, TypeError, ValueError) as e:
            self.close_connection = True
            self._send(400, {'error': f'{type(e).__name__}: {e}'})

    def do_GET(self):
        self._handle(self._get)

    def do_PUT(self):
        self._handle(self._put)

    def do_POST(self):
        self._handle(self._post)

    def _send_blob(self, h):
        fp = self.coord.blob_path(h)
        if not HEX64.match(h) or not fp.exists():
            raise HttpError(404, 'no blob')
        size = fp.stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with open(fp, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                self.wfile.write(chunk)

    def _recv_blob(self, want, who):
        if not HEX64.match(want):
            raise HttpError(400, 'bad hash')
        n = int(self.headers['Content-Length'])
        p = self.coord.blob_path(want)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f'{want}.{who}.{threading.get_ident()}.part'
        h = hashlib.sha256()
        with open(tmp, 'wb') as g:
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                h.update(chunk)
                g.write(chunk)
                left -= len(chunk)
        if h.hexdigest() != want:
            tmp.unlink()
            raise HttpError(400, 'hash mismatch')
        tmp.replace(p)
        self._send(200, {'ok': True})

    def _get(self):
        u = urlparse(self.path)
        p, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        co = self.coord
        if p.startswith('/admin/'):
            self._admin()
            parts = p.split('/')[2:]
            if len(parts) == 2 and parts[0] == 'blob':
                return self._send_blob(parts[1])
            if parts == ['nodes']:
                return self._send(200, co.read(co.node_list, time.time()))
            if parts == ['jobs']:
                return self._send(200, co.read(co.job_list))
            if len(parts) == 2 and parts[0] == 'jobs':
                return self._send(200, co.tx(co.job_view, parts[1], q.get('events') == '1'))
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'merge':
                return self._send(200, co.merge(parts[1]))
            raise HttpError(404, 'unknown')
        self._node()
        if p.startswith('/api/blob/'):
            return self._send_blob(p.rsplit('/', 1)[1])
        if p == '/api/ping':
            return self._send(200, {'t': time.time()})
        raise HttpError(404, 'unknown')

    def _put(self):
        if self.path.startswith('/admin/blob/'):
            self._admin()
            return self._recv_blob(self.path.rsplit('/', 1)[1], 'admin')
        node = self._node()
        if not self.path.startswith('/api/blob/'):
            raise HttpError(404, 'unknown')
        self._recv_blob(self.path.rsplit('/', 1)[1], node)

    def _post(self):
        co = self.coord
        p = urlparse(self.path).path
        if p.startswith('/admin/'):
            self._admin()
            b = self._json()
            parts = p.split('/')[2:]
            if len(parts) == 3 and parts[0] == 'nodes':
                name, what = parts[1], parts[2]
                if what == 'enable':
                    if not isinstance(b.get('enabled'), bool):
                        raise HttpError(400, 'enabled must be boolean')
                    return self._send(200, co.tx(co.set_enabled, name, b['enabled']))
                if what == 'stop':
                    return self._send(200, co.tx(co.stop_now, name))
                if what == 'rescan':
                    return self._send(200, co.tx(co.request_rescan, name))
                if what == 'hold':
                    sec = b.get('seconds')
                    if isinstance(sec, bool) or not isinstance(sec, (int, float)) or not 0 <= sec <= HOLD_MAX_S:
                        raise HttpError(400, f'seconds must be between 0 and {HOLD_MAX_S}')
                    return self._send(200, co.tx(co.set_hold, name, float(sec), str(b.get('why') or '')[:200]))
            if parts == ['jobs']:
                return self._send(200, co.create_job(b))
            if parts == ['plan']:
                return self._send(200, co.read(co.plan, b, time.time()))
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'cancel':
                return self._send(200, co.tx(co.cancel_job, parts[1]))
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'purge':
                return self._send(200, co.tx(co.purge, parts[1]))
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'withdraw':
                return self._send(200, co.tx(co.withdraw, parts[1]))
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'activate':
                return self._send(200, co.tx(co.activate, parts[1]))
            raise HttpError(404, 'unknown')
        node = self._node()
        b = self._json()
        now = time.time()
        route = {'/api/register': lambda c: co.register(c, node, b, self.client_address[0], now),
                 '/api/heartbeat': lambda c: co.heartbeat(c, node, b, now),
                 '/api/caps': lambda c: co.update_caps(c, node, b, now),
                 '/api/claim': lambda c: co.claim(c, node, now, b),
                 '/api/progress': lambda c: co.progress(c, node, b, now),
                 '/api/done': lambda c: co.done(c, node, b, now)}.get(p)
        if not route:
            raise HttpError(404, 'unknown')
        if p not in ('/api/register',):
            known = co.read(lambda c: c.execute('SELECT proto FROM nodes WHERE name=?', (node,)).fetchone())
            if not known or known['proto'] != PROTO:
                raise HttpError(409, 'Node not registered')
        out = co.tx(route)
        if out is None:
            return self._send(204, body=b'')
        self._send(200, out)


def make_server(coord, host, port):
    h = type('BoundHandler', (Handler,), {'coord': coord})
    srv = ThreadingHTTPServer((host, port), h)
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--var', default=os.environ.get('PMX_COORD_VAR', '~/pmx-coord'))
    ap.add_argument('--host', default='10.0.0.10')
    ap.add_argument('--port', type=int, default=8991)
    a = ap.parse_args(argv)
    co = Coordinator(a.var)
    srv = make_server(co, a.host, a.port)
    print(f'coordinator on {a.host}:{a.port}, var {co.var}, proto {PROTO}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
