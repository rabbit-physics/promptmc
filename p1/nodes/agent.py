#!/usr/bin/env python3
"""Compute node worker agent: registers solver and data sha256 manifests, claims shards, runs solvers
at low priority, uploads outputs, and reports telemetry and progress via heartbeats.
Whole-case jobs run with assigned thread counts, report progress every few seconds (OpenMC batch or
Reference MC cycle), and upload intermediate statepoints at job-configured intervals.

Usage: python3 -I agent.py node.json

Uses Python stdlib only; distributed as a single self-contained file. Working directory files:
  PAUSE      hold: stop claiming new shards while active ones continue;
             suspend: SIGSTOP all active shards and stop claiming;
             suspend N: SIGSTOP newest processes totaling >= N slots (resuming oldest first when N shrinks),
                        while allowing backfill claims only up to the pre-existing active capacity cap
                        (written every 30 s by Windows controller; ignored if stale > 120 s)
  host.json  host status (user presence, E-core pinning, AC power) forwarded on heartbeats
  agent.json slot counts, active process count, compute slots, suspended slots, and per-level resume map
"""
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

PROTO = 4
HB_S = 15
# Abort running solvers if coordinator is unreachable for this long: leases have already expired and
# work was likely replicated elsewhere; coordinator uses the same deadline for stop confirmation.
RENEW_LIMIT_S = 600
PAUSE_FRESH_S = 120 # Ignore PAUSE older than this so shards resume automatically if controller exits
REPORT_BACKOFF = (15, 30, 60)   # Retry intervals (s) when reporting results fails
BLOB_CACHE_MB = 2048            # LRU blob cache cap; active inputs are pinned while read
RESCAN_S = 120      # Periodically rescan data_root so newly added data files appear without restart
HOST_FRESH_S = 120  # Stop forwarding host.json if stale beyond this window
PROGRESS_S = 5      # Whole-case progress poll interval (also delivers coordinator cancellation)
UNITS = {'ref_mc': 'hist', 'openmc': 'particle'}
OPENMC_BATCH = re.compile(r'^\s+(\d+)/(\d+)\s+([0-9.]+)(?:\s+([0-9.]+)\s+\+/-\s+([0-9.]+))?\s*$')
REF_MC_CYCLE = re.compile(r'^\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)(?:\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)\s+\d+)?\s*$')
SP_NAME = re.compile(r'^statepoint\.(\d+)\.h5$')


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def sha_file(p, cache):
    """Cached sha256 keyed by (path, size, mtime_ns)."""
    st = p.stat()
    key = str(p)
    hit = cache.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    cache[key] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
    return cache[key][2]


def _read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


def log_tail(p, n=8192):
    try:
        with open(p, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def parse_progress(solver, text):
    """Parse last progress line: OpenMC batch/k/sigma or Reference MC cycle/k/sigma."""
    last = None
    for line in text.splitlines():
        m = (OPENMC_BATCH if solver == 'openmc' else REF_MC_CYCLE).match(line)
        if m:
            last = m
    if not last:
        return None
    g = last.groups()
    if solver == 'openmc':
        return {'batch': int(g[0]), 'k_batch': float(g[2]), 'k': float(g[3]) if g[3] else None,
                'sigma': float(g[4]) if g[4] else None}
    return {'cycle': int(g[0]), 'k_cycle': float(g[1]), 'k': float(g[4]) if g[4] else None, 'sigma': float(g[5]) if g[5] else None}


def statepoints(d):
    """Return sorted [(batch, Path)] for statepoint.*.h5 files in d."""
    out = []
    for p in d.glob('statepoint.*.h5'):
        m = SP_NAME.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def resources(root='/'):
    """Memory limit (min of cgroup and host RAM), available CPUs (affinity vs cgroup quota), and 1-min load."""
    root = Path(root)
    mem = None
    for line in (_read(root / 'proc/meminfo') or '').splitlines():
        if line.startswith('MemTotal:'):
            mem = int(line.split()[1]) * 1024
    cpus = float(len(os.sched_getaffinity(0))) if root == Path('/') else float(os.cpu_count() or 1)
    rel = None
    for line in (_read(root / 'proc/self/cgroup') or '').splitlines():
        if line.startswith('0::'):
            rel = line[3:].strip('/')
    if rel is not None:
        d = root / 'sys/fs/cgroup' / rel
        base = root / 'sys/fs/cgroup'
        while True:
            m = _read(d / 'memory.max')
            if m and m != 'max':
                mem = min(mem or int(m), int(m))
            c = (_read(d / 'cpu.max') or '').split()
            if len(c) == 2 and c[0] != 'max':
                cpus = min(cpus, int(c[0]) / int(c[1]))
            if d == base:
                break
            d = d.parent
    try:
        load1 = os.getloadavg()[0] if root == Path('/') else None
    except OSError:
        load1 = None
    return {'mem_bytes': mem, 'cpus': round(cpus, 2), 'ncpu': os.cpu_count(), 'load1': load1}


class Agent:
    def __init__(self, cfg, stop=None):
        self.cfg = cfg
        self.work = Path(cfg['work']).expanduser()
        self.blobs = self.work / 'blobs'
        self.hb_s = cfg.get('hb_s', HB_S)
        self.running = {}
        # Active shards mapped to (threads, mem_bytes)
        self.held = {}
        self.started = {}       # Start timestamps; newest processes are suspended first
        self.stopped = []       # Currently SIGSTOPped shard keys in suspension order
        self.susp = {}          # key -> [accumulated_s, current_suspend_start_or_None]
        self.want = 0           # Last PAUSE target slots from parse_pause
        self.cap = None         # Active slot cap during partial suspension
        self.leases = {}        # (job, idx) -> coordinator-issued lease_id
        self.delivering = set() # In-flight (job, idx, lease) deliveries excluded from re-claim
        self.epoch, self.hb_seq, self.acked_claim = None, 0, 0
        self.cancelled = set()  # Shards killed due to cancellation or coordinator disconnect (outputs omitted)
        self.using = {}         # Active blob readers preventing cache eviction during copy
        self.blob_lock = threading.Lock()
        self.last_contact = time.monotonic()
        self.pause_stale = False
        self.rlock = threading.RLock()
        self.last_suspended = 0
        self.last_stopped = set()
        self.wake = threading.Event()
        self.stop = stop or threading.Event()
        self.recent = deque(maxlen=8)
        self.caps = None
        self.caps_lock = threading.Lock()
        self.rescan_now = threading.Event()
        try:
            self.agent_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        except OSError:
            self.agent_sha = ''

    # ---------- Coordinator Communication ----------
    def req(self, method, path, obj=None, data=None, timeout=60):
        body = data if data is not None else (json.dumps(obj).encode() if obj is not None else None)
        r = urllib.request.Request(self.cfg['coord'] + path, data=body, method=method,
                                   headers={'X-Node': self.cfg['node'], 'X-Token': self.cfg['token'], 'X-Proto': str(PROTO),
                                            'Content-Type': 'application/json' if data is None else 'application/octet-stream'})
        sent = time.monotonic()
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
                result = json.loads(raw) if raw and resp.headers.get('Content-Type') == 'application/json' else raw
                with self.rlock:
                    self.last_contact = max(self.last_contact, sent)
                return resp.status, result
        except (http.client.HTTPException, ValueError) as e:
            raise OSError(f'incomplete coordinator response: {type(e).__name__}') from e

    @staticmethod
    def why(e):
        if isinstance(e, urllib.error.HTTPError):
            try:
                return f'{e.code} ' + json.loads(e.read()).get('error', '')
            except Exception:
                return f'{e.code}'
        return repr(e)

    def capabilities(self):
        cache_p = self.work / 'hashcache.json'
        try:
            cache = json.loads(cache_p.read_text())
        except (OSError, ValueError):
            cache = {}
        solvers = {}
        for name, s in self.cfg['solvers'].items():
            ident = {Path(p).name: sha_file(Path(p).expanduser(), cache) for p in s['identity']}
            exe = hashlib.sha256(''.join(ident[Path(p).name] for p in s['identity']).encode()).hexdigest()
            root = Path(s['data_root']).expanduser()
            files = {}
            for p in sorted(root.rglob('*')):
                if p.is_file() and p.name not in s.get('skip', []):
                    files[str(p.relative_to(root))] = sha_file(p, cache)
            solvers[name] = {'exe': exe, 'ident': ident, 'files': files}
        tmp = cache_p.with_suffix('.tmp')
        tmp.write_text(json.dumps(cache))
        tmp.replace(cache_p)
        return {'solvers': solvers, 'host': socket.gethostname(), 'slots': self.cfg['slots'], 'cpu_count': os.cpu_count(),
                'hb_s': self.hb_s, 'agent': {'proto': PROTO, 'sha': self.agent_sha}}

    def rescan(self):
        """Recompute data inventory and push updates to the coordinator if changed."""
        with self.caps_lock:
            caps = self.capabilities()
            if caps['solvers'] == self.caps['solvers']:
                return False
            self.req('POST', '/api/caps', {'caps': caps})
            self.caps = caps
        log('capabilities updated', {k: len(v['files']) for k, v in caps['solvers'].items()})
        return True

    def rescan_loop(self):
        while not self.stop.is_set():
            self.rescan_now.wait(RESCAN_S)
            self.rescan_now.clear()
            if self.stop.is_set():
                return
            try:
                self.rescan()
            except Exception as e:
                log('rescan failed', self.why(e))

    def register(self):
        if self.epoch is None:
            self.work.mkdir(parents=True, exist_ok=True)
            p = self.work / 'epoch'
            try:
                previous = int(p.read_text())
            except (OSError, ValueError):
                previous = 0
            self.epoch = max(time.time_ns(), previous + 1)
            tmp = p.with_suffix('.tmp')
            tmp.write_text(str(self.epoch))
            tmp.replace(p)
        while not self.stop.is_set():
            try:
                self.req('POST', '/api/register', {'caps': self.caps, 'epoch': self.epoch})
                log('registered', self.cfg['node'], 'proto', PROTO, {k: len(v['files']) for k, v in self.caps['solvers'].items()})
                return True
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                log('register failed, retrying', self.why(e))
                self.stop.wait(20)
        return False

    # ---------- Host Yield & Suspension State ----------
    def pause_state(self):
        """Read PAUSE file; ignore if older than PAUSE_FRESH_S so suspended shards resume cleanly."""
        p = self.work / 'PAUSE'
        try:
            text = p.read_text().strip()
            stale = time.time() - p.stat().st_mtime > PAUSE_FRESH_S
        except OSError:
            self.pause_stale = False
            return ''
        if stale != self.pause_stale:
            log('PAUSE not rewritten for over', PAUSE_FRESH_S, 's, ignored' if stale else 'fresh again:', text)
            self.pause_stale = stale
        return '' if stale else text

    @staticmethod
    def parse_pause(text):
        """Parse PAUSE text -> target suspended slots (0 = none, None = suspend all)."""
        t = text.split()
        if not t or t[0] != 'suspend':
            return 0
        if len(t) == 1:
            return None
        try:
            return max(0, int(t[1]))
        except ValueError:
            return None

    def apply_suspend(self, want):
        with self.rlock:
            self._apply_suspend(want)
            self.note_suspension(want)

    def _apply_suspend(self, want):
        """Ensure at least want slots are SIGSTOPped (None = all): suspend newest processes first until
        >= want slots are held; when want drops, resume longest-suspended processes first while keeping
        >= want slots suspended (preventing any single shard from staying paused long enough to trigger
        cross-node replication). Already-exited processes uploading outputs are never suspended."""
        with self.rlock:
            live = {k: (self.started.get(k, 0.0), p, self.held.get(k, (1, None))[0]) for k, p in self.running.items()
                    if p and p.returncode is None}
        before = list(self.stopped)
        self.stopped = [k for k in self.stopped if k in live]
        total = sum(v[2] for v in live.values())
        n = total if want is None else min(want, total)

        def held():
            return sum(live[k][2] for k in self.stopped)
        for k in list(self.stopped):
            if held() - live[k][2] >= n:
                self.stopped.remove(k)
                self._signal(live[k][1], signal.SIGCONT)
                self._mark(k, False)
        if held() < n:
            for k in sorted((k for k in live if k not in self.stopped), key=lambda k: live[k][0], reverse=True):
                if held() >= n:
                    break
                self._signal(live[k][1], signal.SIGSTOP)
                self.stopped.append(k)
                self._mark(k, True)
        if self.stopped != before:
            log('suspended', len(self.stopped), 'of', len(live), 'running,', held(), 'of', total, 'slots')

    def _mark(self, key, on):
        """Track suspension intervals: on=True starts timing, False closes the interval."""
        now = time.time()
        with self.rlock:
            e = self.susp.get(key)
            if on:
                p = self.running.get(key)
                if p and p.returncode is None and (e is None or e[1] is None):
                    self.susp[key] = [e[0] if e else 0.0, now]
            elif e and e[1] is not None:
                e[0] += now - e[1]
                e[1] = None

    def suspended_s(self, key, pop=False):
        with self.rlock:
            e = self.susp.pop(key, None) if pop else self.susp.get(key)
        if not e:
            return 0.0
        return e[0] + (time.time() - e[1] if e[1] is not None else 0.0)

    def slots_used(self):
        with self.rlock:
            return sum(t for t, _ in self.held.values())

    def suspended_slots(self):
        with self.rlock:
            return sum(self.held.get(k, (1, None))[0] for k in self.stopped)

    def note_suspension(self, want):
        """Record active slot capacity when suspension target changes; tighten cap if suspended slots grow."""
        with self.rlock:
            active, suspended = self.active_slots(), self.suspended_slots()
            if want != self.want:
                self.cap = active if want else None
                self.want = want
            elif self.cap is not None and (suspended > self.last_suspended or set(self.stopped) - self.last_stopped):
                self.cap = min(self.cap, active)
            self.last_suspended = suspended
            self.last_stopped = set(self.stopped)

    def active_slots(self):
        """Slots currently solving or starting (unsuspended); exited uploaders do not consume compute capacity."""
        with self.rlock:
            return sum(self.held.get(k, (1, None))[0] for k, p in self.running.items()
                       if k not in self.stopped and (p is None or p.returncode is None))

    def room(self):
        """Remaining claimable slots during partial suspension."""
        with self.rlock:
            return max(0, self.cap - self.active_slots()) if self.cap is not None else 0

    def claim_free(self, text, want):
        """Claimable slots right now: all free slots if unpaused, <= room() under suspend N, else 0."""
        if not self.may_claim(text, want):
            return 0
        free = self.free_slots()
        return min(free, self.room()) if text and want else free

    def may_claim(self, text, want):
        """True when unpaused, or under partial suspend N while room() > 0 to backfill finished shards."""
        if not text:
            return True
        if text.split()[0] != 'suspend' or want is None:
            return False
        return want == 0 or self.room() > 0

    def state(self, text, want):
        """Heartbeat state: active (including partial suspend with backfill room), suspend (all), or hold."""
        if self.may_claim(text, want):
            return 'active'
        if text.split()[0] == 'suspend' and want is None:
            return 'suspend'
        return 'hold'

    def agent_state(self):
        with self.rlock:
            paused = [self.held.get(k, (1, None))[0] for k in self.stopped
                      if k in self.running and self.running[k] and self.running[k].returncode is None]
            resumed = {}
            for target in range(self.cfg['slots'] + 1):
                left, released = sum(paused), 0
                for slots in paused:
                    if left - slots >= target:
                        left -= slots
                        released += slots
                resumed[str(target)] = released
            return {'slots': self.cfg['slots'], 'running': len(self.running), 'suspended': len(paused),
                    'slots_used': self.slots_used(), 'compute_slots': self.active_slots() + sum(paused),
                    'suspended_slots': sum(paused), 'resume_slots': resumed}

    def write_agent_state(self, last):
        st = self.agent_state()
        if st == last:
            return last
        p = self.work / 'agent.json'
        try:
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps(st))
            tmp.replace(p)
            return st
        except OSError as e:
            log('agent.json write failed', repr(e))
            return last

    def host_state(self):
        p = self.work / 'host.json'
        try:
            if time.time() - p.stat().st_mtime > HOST_FRESH_S:
                return None
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def rate(self):
        """Measured throughput of the most recent job: per-slot = sum(work)/sum(active_wall), node = per_slot * slots."""
        with self.rlock:
            recent = list(self.recent)
        if not recent:
            return None
        job, solver = recent[-1][0], recent[-1][1]
        w = sum(x[2] for x in recent if x[0] == job)
        t = sum(x[3] for x in recent if x[0] == job)
        if t <= 0:
            return None
        return {'job': job, 'solver': solver, 'unit': UNITS.get(solver, 'unit'), 'per_slot': w / t,
                'per_s': w / t * self.cfg['slots']}

    def res(self):
        r = resources()
        r['rate'] = self.rate()
        h = self.host_state()
        # Coordinator forwards host verbatim to the UI; attach suspended slot count here for visibility
        if self.stopped:
            h = dict(h or {}, suspended=self.suspended_slots())
        if h is not None:
            r['host'] = h
        return r

    # ---------- Shard Execution ----------
    def free_slots(self):
        with self.rlock:
            return self.cfg['slots'] - sum(t for t, _ in self.held.values())

    def mem_free(self):
        mem = resources().get('mem_bytes')
        if not mem:
            return None
        with self.rlock:
            return mem - sum(m or 0 for _, m in self.held.values())

    def fetch_into(self, h, dest):
        """Copy cached input blob to working dir while pinning it against cache eviction."""
        with self.blob_lock:
            self.using[h] = self.using.get(h, 0) + 1
        try:
            Path(dest).write_bytes(self.fetch(h).read_bytes())
        finally:
            with self.blob_lock:
                self.using[h] -= 1
                if not self.using[h]:
                    del self.using[h]
        self.trim_blobs()

    def trim_blobs(self):
        """Evict least-recently-used blobs above cap, skipping files currently pinned in self.using."""
        cap = self.cfg.get('blob_cache_mb', BLOB_CACHE_MB) * 1048576
        with self.blob_lock:
            try:
                files = [(q.stat().st_mtime, q.stat().st_size, q) for q in self.blobs.iterdir() if '.part' not in q.name]
            except OSError:
                return
            total = sum(x[1] for x in files)
            for _, size, q in sorted(files):
                if total <= cap:
                    break
                if q.name in self.using:
                    continue
                try:
                    q.unlink()
                    total -= size
                except OSError:
                    pass

    def fetch(self, h):
        p = self.blobs / h
        if p.exists():
            try:
                os.utime(p)
            except OSError:
                pass
            return p
        self.blobs.mkdir(parents=True, exist_ok=True)
        tmp = self.blobs / f'{h}.part.{threading.get_ident()}'
        r = urllib.request.Request(f"{self.cfg['coord']}/api/blob/{h}",
                                   headers={'X-Node': self.cfg['node'], 'X-Token': self.cfg['token'], 'X-Proto': str(PROTO)})
        d = hashlib.sha256()
        with urllib.request.urlopen(r, timeout=300) as resp, open(tmp, 'wb') as g:
            for chunk in iter(lambda: resp.read(1 << 20), b''):
                d.update(chunk)
                g.write(chunk)
        if d.hexdigest() != h:
            tmp.unlink()
            raise IOError(f'blob {h} hash mismatch')
        tmp.replace(p)
        return p

    def upload(self, p):
        data = p.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        self.req('PUT', f'/api/blob/{h}', data=data, timeout=300)
        return h

    @staticmethod
    def rmtree(d):
        for p in sorted(d.rglob('*'), reverse=True):
            p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()
        d.rmdir()

    def run_shard(self, job):
        key = (job['job'], job['idx'])
        js, sh = job['job_spec'], job['shard']
        d = self.work / 'shards' / f"{job['job']}-{job['idx']:04d}-{job.get('lease') or 'legacy'}"
        if d.exists():
            self.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        rc, cpu, wall, outputs = 1, 0.0, 0.0, {}
        try:
            for name, h in sh['inputs'].items():
                self.fetch_into(h, d / name)
            s = self.cfg['solvers'][js['solver']]
            env = dict(os.environ, **{k: str(Path(v).expanduser()) for k, v in s.get('env', {}).items()})
            # Ensure child solver is killed immediately if the agent exits
            cmd = ['setpriv', '--pdeathsig', 'KILL', 'nice', '-n', str(self.cfg.get('nice', 19)), str(Path(s['exe']).expanduser())] + js['args']
            t0 = time.time()
            with open(d / 'stdout.log', 'wb') as out:
                p = subprocess.Popen(cmd, cwd=d, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
            with self.rlock:
                self.running[key] = p
                self.started[key] = t0
            if key in self.cancelled:
                # Cancelled while inputs were downloading
                self._signal(p, signal.SIGKILL)
            _, status, ru = os.wait4(p.pid, 0)
            self._exited(key, p, status)
            wall, cpu = time.time() - t0, ru.ru_utime + ru.ru_stime
            rc = p.returncode
            for name in js['outputs'] if key not in self.cancelled else []:
                hits = sorted(d.glob(name))
                if not hits:
                    rc = rc or 2
                    continue
                outputs[hits[-1].name] = hashlib.sha256(hits[-1].read_bytes()).hexdigest()
        except Exception as e:
            log('shard', key, 'error', repr(e))
            rc = rc or 3
        finally:
            with self.rlock:
                if self.leases.get(key) == job.get('lease'):
                    self.running.pop(key, None)
                    self.started.pop(key, None)
        self.report(job, d, outputs, cpu, wall, rc)

    def _exited(self, key, p, status):
        """Record exit status and close suspension timing when solver exits (before output upload)."""
        with self.rlock:
            p.returncode = os.waitstatus_to_exitcode(status)
            if self.running.get(key) is p:
                self._mark(key, False)

    def report(self, job, d, outputs, cpu, wall, rc):
        """Deliver shard results; persist to pending/ so retries survive agent restarts."""
        key = (job['job'], job['idx'])
        js, sh = job['job_spec'], job['shard']
        threads = job.get('threads') or 1
        with self.rlock:
            own = self.leases.get(key) == job.get('lease')
            susp = self.suspended_s(key, pop=True) if own else 0.0
            cancelled = key in self.cancelled if own else False
            if own:
                self.cancelled.discard(key)
            delivery = (*key, job.get('lease'))
            self.delivering.add(delivery)
        body = {'job': job['job'], 'idx': job['idx'], 'outputs': outputs, 'cpu_s': cpu, 'wall_s': wall, 'rc': rc, 'threads': threads,
                'suspended_s': round(susp, 1), 'lease': job.get('lease'), 'cancelled': cancelled}
        pend = self.pending_put(body, d)
        try:
            while pend is None and not self.stop.wait(REPORT_BACKOFF[0]):
                pend = self.pending_put(body, d)
            if pend is None:
                return
            r = self.deliver(body, d)
            if r is None:
                return
            log('shard', key, 'rc', rc, f'cpu {cpu:.1f}s wall {wall:.1f}s suspended {susp:.1f}s', r)
            # Compute rate from active solve time excluding suspended intervals
            if rc == 0 and wall - susp > 0 and r.get('accepted'):
                with self.rlock:
                    self.recent.append((job['job'], js['solver'], (sh.get('work') or 0) / threads, wall - susp))
            if pend:
                pend.unlink(missing_ok=True)
        finally:
            with self.rlock:
                self.delivering.discard(delivery)
                if self.leases.get(key) == job.get('lease'):
                    self.held.pop(key, None)
                    self.leases.pop(key, None)
            self.wake.set()

    # Terminal rejections whose working dirs can be removed immediately; failed shards keep dirs for inspection
    FINAL = ('already done', 'not lease holder', 'cancelled', 'cancelled by node', 'failed', 'unknown shard')

    def deliver(self, body, d):
        """Upload outputs and POST /api/done until coordinator responds (or None on agent shutdown)."""
        n, uploaded = 0, False
        while not self.stop.is_set():
            try:
                if not uploaded:
                    for name, expected in body['outputs'].items():
                        if self.upload(Path(d) / name) != expected:
                            raise OSError(f'output changed before delivery: {name}')
                    uploaded = True
                _, r = self.req('POST', '/api/done', body)
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    log('done report rejected', self.why(e))
                    return {'accepted': False, 'why': 'rejected'}
                log('done report refused, retrying', self.why(e))
                r = None
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                log('done report failed, retrying', self.why(e))
                r = None
            if r is not None:
                if str(r.get('why', '')).startswith('missing blob') and d and Path(d).exists():
                    uploaded = False
                    if self.stop.wait(REPORT_BACKOFF[min(n, len(REPORT_BACKOFF) - 1)]):
                        return None
                    n += 1
                    continue
                if r.get('accepted') or r.get('why') in self.FINAL:
                    if d and Path(d).exists():
                        self.rmtree(Path(d))
                return r
            if self.stop.wait(REPORT_BACKOFF[min(n, len(REPORT_BACKOFF) - 1)]):
                return None
            n += 1
        return None

    def pending_put(self, body, d):
        p = self.work / 'pending' / f"{body['job']}-{body['idx']:04d}-{body.get('lease') or 'x'}.json"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps({'body': body, 'dir': str(d)}))
            tmp.replace(p)
            return p
        except OSError as e:
            log('pending result write failed', repr(e))
            return None

    def deliver_pending(self):
        """Flush any undelivered results persisted before a prior agent restart."""
        for p in sorted((self.work / 'pending').glob('*.json')):
            try:
                x = json.loads(p.read_text())
            except (OSError, ValueError):
                p.unlink(missing_ok=True)
                continue
            r = self.deliver(x['body'], x.get('dir'))
            if r is None:
                return
            log('pending result', x['body']['job'], x['body']['idx'], r)
            p.unlink(missing_ok=True)

    def mine(self):
        """Return (job, idx) keys currently running or awaiting delivery so they are not re-claimed locally."""
        with self.rlock:
            keys = set(self.running) | set(self.held) | {(j, i) for j, i, _ in self.delivering}
        for p in (self.work / 'pending').glob('*.json'):
            try:
                b = json.loads(p.read_text())['body']
                keys.add((b['job'], b['idx']))
            except (OSError, ValueError, KeyError):
                continue
        return [list(k) for k in sorted(keys)]

    def run_case(self, job):
        """Whole-case execution: runs unmodified input with assigned thread count, posts progress every
        PROGRESS_S seconds, honors coordinator cancel, and uploads completed statepoints for cross-node resume."""
        key = (job['job'], job['idx'])
        js, sh = job['job_spec'], job['shard']
        threads = job.get('threads') or 1
        d = self.work / 'shards' / f"{job['job']}-{job['idx']:04d}-{job.get('lease') or 'legacy'}"
        if d.exists():
            self.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        rc, cpu, wall, outputs = 1, 0.0, 0.0, {}
        try:
            for name, h in sh['inputs'].items():
                self.fetch_into(h, d / name)
            s = self.cfg['solvers'][js['solver']]
            env = dict(os.environ, **{k: str(Path(v).expanduser()) for k, v in s.get('env', {}).items()}, **(js.get('env') or {}))
            args = []
            for a in js['args']:
                if a == '{restart}':
                    args += ['-r', sh['restart']] if sh.get('restart') else []
                else:
                    args.append(a.replace('{threads}', str(threads)))
            cmd = ['setpriv', '--pdeathsig', 'KILL', 'nice', '-n', str(self.cfg.get('nice', 19)), str(Path(s['exe']).expanduser())] + args
            logp = d / js.get('log', 'stdout.log')
            t0 = time.time()
            with open(logp, 'wb') as out:
                p = subprocess.Popen(cmd, cwd=d, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
            with self.rlock:
                self.running[key] = p
                self.started[key] = t0
            if key in self.cancelled:
                self._signal(p, signal.SIGKILL)
            last_post, last_ckpt, sent_batch = 0.0, t0, sh.get('restart_batch', 0)
            while True:
                pid, status, ru = os.wait4(p.pid, os.WNOHANG)
                if pid:
                    break
                now = time.time()
                if now - last_post >= PROGRESS_S:
                    last_post = now
                    prog = parse_progress(js['solver'], log_tail(logp)) or {}
                    prog['wall_s'] = round(now - t0, 1)
                    with self.rlock:
                        e = self.susp.get(key)
                    # Report yield suspension status/duration; coordinator replicates if suspended too long
                    prog['suspended'] = bool(e and e[1] is not None)
                    prog['suspended_s'] = round(self.suspended_s(key), 1)
                    sp = None
                    if js.get('ckpt_s') and now - last_ckpt >= js['ckpt_s']:
                        # Only upload a statepoint once log progress proves the next batch has begun
                        done_sps = [x for x in statepoints(d) if x[0] < (prog.get('batch') or 0)]
                        if done_sps and done_sps[-1][0] > sent_batch:
                            b, path = done_sps[-1]
                            try:
                                sp = {'batch': b, 'name': path.name, 'sha': self.upload(path)}
                                sent_batch, last_ckpt = b, now
                            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                                log('statepoint upload failed', key, self.why(e))
                    try:
                        _, r = self.req('POST', '/api/progress', {'job': job['job'], 'idx': job['idx'], 'progress': prog, 'sp': sp,
                                                                  'lease': job.get('lease')})
                        if r.get('cancel') and key not in self.cancelled:
                            self.kill(key)
                    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                        log('progress report failed', key, self.why(e))
                time.sleep(0.5)
            self._exited(key, p, status)
            wall, cpu = time.time() - t0, ru.ru_utime + ru.ru_stime
            rc = p.returncode
            # Omit outputs when cancelled by coordinator or aborted after lost contact
            if key not in self.cancelled:
                for name in js['outputs']:
                    hits = [x[1] for x in statepoints(d)] if name.startswith('statepoint') else sorted(d.glob(name))
                    if not hits:
                        rc = rc or 2
                        continue
                    # Preserve actual filename (e.g. statepoint.<batch>.h5)
                    outputs[hits[-1].name] = hashlib.sha256(hits[-1].read_bytes()).hexdigest()
        except Exception as e:
            log('case', key, 'error', repr(e))
            rc = rc or 3
        finally:
            with self.rlock:
                if self.leases.get(key) == job.get('lease'):
                    self.running.pop(key, None)
                    self.started.pop(key, None)
        self.report(job, d, outputs, cpu, wall, rc)

    @staticmethod
    def _signal(p, sig):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            pass

    def kill(self, key, why='lease lost'):
        """Terminate a running solver whose lease was revoked, cancelled, or lost contact; omits outputs."""
        with self.rlock:
            p = self.running.get(key)
            # Allow already-exited jobs currently uploading results to finish reporting
            if key in self.running and not (p and p.returncode is not None):
                self.cancelled.add(key)
        if p and p.returncode is None:
            log(why + ', killing', *key)
            try:
                os.killpg(p.pid, signal.SIGCONT)
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def give_up_if_lost(self):
        """Kill active runs after RENEW_LIMIT_S without coordinator contact (matches coordinator expiry)."""
        with self.rlock:
            if not self.running or time.monotonic() - self.last_contact <= RENEW_LIMIT_S:
                return []
            keys = list(self.running)
        out = []
        for key in keys:
            if key not in self.cancelled:
                self.kill(key, f'no contact with the coordinator for {RENEW_LIMIT_S} s')
                out.append(key)
        return out

    def contact_watch(self):
        """Enforce disconnect timeout independently even while network calls block."""
        while not self.stop.wait(0.2):
            self.give_up_if_lost()

    # ---------- Main Loop ----------
    def run(self):
        self.work.mkdir(parents=True, exist_ok=True)
        self.caps = self.capabilities()
        threading.Thread(target=self.contact_watch, daemon=True).start()
        if not self.register():
            return
        threading.Thread(target=self.rescan_loop, daemon=True).start()
        threading.Thread(target=self.deliver_pending, daemon=True).start()
        last_hb, last_st = 0.0, None
        while not self.stop.is_set():
            text = self.pause_state()
            want = self.parse_pause(text)
            # Re-evaluate every tick so newly launched processes obey active suspension targets
            self.apply_suspend(want)
            self.note_suspension(want)
            lost = time.monotonic() - self.last_contact > RENEW_LIMIT_S
            if time.time() - last_hb > self.hb_s or lost:
                with self.rlock:
                    running = [[k[0], k[1], self.leases.get(k), k in self.stopped] for k in self.running]
                    self.hb_seq += 1
                    snapshot = {'epoch': self.epoch, 'seq': self.hb_seq, 'acked_claim': self.acked_claim}
                try:
                    _, r = self.req('POST', '/api/heartbeat', {'running': running, 'state': self.state(text, want), 'res': self.res(),
                                                             'free': self.claim_free(text, want), 'capacity': self.cap, **snapshot})
                    with self.rlock:
                        self.acked_claim = max(self.acked_claim, r.get('claim_seq', 0))
                    for job, idx in r.get('cancel', []):
                        self.kill((job, idx))
                    if r.get('rescan'):
                        self.rescan_now.set()
                    last_hb = time.time()
                except urllib.error.HTTPError as e:
                    log('heartbeat refused', self.why(e))
                    if e.code == 409:
                        self.register()
                except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                    log('heartbeat failed', self.why(e))
            self.give_up_if_lost()
            while self.claim_free(text, want) > 0 and not self.stop.is_set():
                mine = self.mine()
                try:
                    code, job = self.req('POST', '/api/claim', {'free': self.claim_free(text, want), 'mem': self.mem_free(),
                                                             'running': mine, 'epoch': self.epoch})
                except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
                    log('claim failed', self.why(e))
                    break
                if code == 204:
                    break
                key = (job['job'], job['idx'])
                log('claimed', *key, 'threads', job.get('threads') or 1)
                with self.rlock:
                    self.running[key] = None
                    self.leases[key] = job.get('lease')
                    self.held[key] = (job.get('threads') or 1, job['job_spec'].get('mem'))
                    self.acked_claim = max(self.acked_claim, job.get('claim_seq', 0))
                case = job['job_spec'].get('kind') == 'case'
                threading.Thread(target=self.run_case if case else self.run_shard, args=(job,), daemon=True).start()
            last_st = self.write_agent_state(last_st)
            # Wake immediately when a shard completes to claim the next one
            self.wake.wait(3)
            self.wake.clear()
        self.rescan_now.set()


def main():
    Agent(json.loads(Path(sys.argv[1]).read_text())).run()


if __name__ == '__main__':
    main()
