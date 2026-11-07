"""Remote execution bridge: dispatches whole-case solver runs (OpenMC eigenvalue or Reference MC KCODE)
to the compute node coordinator via its admin API (create, status, fetch outputs, stop).

Criticality eigenvalue runs pass fission source sites from generation to generation and cannot be split
across history ranges: each solver run is dispatched as a whole-case job with identical input and seed
to local execution. Falls back to local execution when the coordinator is offline or no eligible node
can accept the job, holding the local worker node during local runs to avoid CPU oversubscription.
"""
import hashlib
import os
import socket
import threading
import time

from ..nodes.client import NodesError

THREADS_MAX = int(os.environ.get('P1_NODE_THREADS', '4'))
THREADS_MIN = int(os.environ.get('P1_NODE_MIN_THREADS', '2'))
CKPT_S = int(os.environ.get('P1_NODE_CKPT_S', '120'))
STOP_WAIT_S = 30.0
HOLD_S = 120


class Remote:
    def __init__(self, client, root, dispatch=True, local_node=None, threads=THREADS_MAX, min_threads=THREADS_MIN,
                 ckpt_s=CKPT_S, stop_wait_s=STOP_WAIT_S, fast=None):
        self.c = client
        # Multiple backend instances (prod/dev) may share one coordinator: namespace job IDs by root hash
        self.prefix = hashlib.sha256(os.path.abspath(root).encode()).hexdigest()[:6]
        self.dispatch = dispatch
        self.local_node = local_node
        self.threads, self.min_threads = threads, min(min_threads, threads)
        self.ckpt_s, self.stop_wait_s = ckpt_s, stop_wait_s
        # Use short timeout for stop, status, and hold calls so coordinator stalls never block watcher threads
        self.fast = fast or type(client)(client.url, client.token_file, timeout=5)
        self._local = (0.0, None)
        self._lock = threading.Lock()

    def job_id(self, tid):
        return f'pmx-{self.prefix}-{tid}-{int(time.time() * 1000) % 10 ** 10}'

    # ---------- Nodes ----------
    def nodes(self):
        """Return node list, or None if the coordinator is unreachable."""
        try:
            return self.fast.nodes()['nodes']
        except NodesError:
            return None

    def local(self):
        """Name of the compute node co-located on this host (configured or matched by hostname), cached 60 s."""
        if self.local_node is not None:
            return self.local_node or None
        with self._lock:
            t, name = self._local
            if time.time() - t < 60:
                return name
        ns = self.nodes()
        if ns is None:
            return name
        host = socket.gethostname()
        name = next((n['name'] for n in ns if n.get('hostname') == host), None)
        with self._lock:
            self._local = (time.time(), name)
        return name

    def hold_local(self, seconds):
        """Hold claims on the local co-located node for seconds (0 releases hold); no-op if unreachable."""
        name = self.local()
        if not name:
            return None
        try:
            self.fast.hold(name, seconds, 'local solve' if seconds else '')
        except NodesError:
            return None
        return name

    def local_busy(self):
        """True if the local co-located node still has active shards (leased or in last heartbeat)."""
        name = self.local()
        if not name:
            return False
        ns = self.nodes()
        n = next((x for x in ns or [] if x['name'] == name), None)
        return bool(n and n.get('online') and (n.get('used') or n.get('running')))

    # ---------- Jobs ----------
    def _body(self, jid, solver, text, est, requires):
        body = {'id': jid, 'kind': 'case', 'solver': solver}
        if est:
            body['est'] = round(float(est), 1)
        if requires:
            body['requires'] = requires
        if solver == 'openmc':
            body.update(model=text, threads=self.threads, min_threads=self.min_threads, ckpt_s=self.ckpt_s)
        else:
            body['deck'] = text
        return body

    def ready_nodes(self, jid, solver, text, est=None, requires=None):
        """Dry-run eligibility check: returns nodes currently able to run the job given est (core-s) and requires."""
        return self.c.create_job(dict(self._body(jid, solver, text, est, requires), dry_run=True)).get('nodes_ready') or []

    def plan(self, jobs):
        """Ask the coordinator to simulate scheduling for hypothetical whole-case jobs."""
        return self.fast.plan(jobs)

    def submit(self, jid, solver, text, restart=None, est=None, requires=None):
        """Create a whole-case job in held state pending activate(). Unactivated jobs expire automatically."""
        body = self._body(jid, solver, text, est, requires)
        if restart:
            body['restart'] = {'name': os.path.basename(restart), 'sha': self.c.put_blob(restart)}
        return self.c.create_job(dict(body, hold=True))

    def activate(self, jid):
        """Release a held job for claiming. Raises NodesError (503/409/404) on failure."""
        return self.fast.activate(jid)

    def status(self, jid):
        return self.fast.job(jid)['case']

    def cancel(self, jid):
        try:
            self.fast.cancel(jid)
            return True
        except NodesError as e:
            return e.code == 404

    def alive(self, jid):
        """True if the job is queued, leased, held, or possibly still running on an unreachable node."""
        try:
            v = self.status(jid)
        except NodesError as e:
            return e.code != 404
        return v['state'] in ('queued', 'leased', 'held') or bool(v['running'])

    def stop(self, jid):
        """Cancel the job and wait up to stop_wait_s for stop confirmation ('stopped' or 'stop_unconfirmed')."""
        if not self.cancel(jid):
            return 'stop_unconfirmed'
        t0 = time.time()
        while time.time() - t0 < self.stop_wait_s:
            if not self.alive(jid):
                return 'stopped'
            time.sleep(1.0)
        return 'stop_unconfirmed'

    def withdraw(self, jid):
        """Withdraw an unclaimed job; returns True if withdrawn, False if already started or unreachable."""
        try:
            return bool(self.fast.withdraw(jid).get('withdrawn'))
        except NodesError:
            return False

    def fetch(self, h, path):
        return self.c.get_blob(h, path)

    def purge(self, jid):
        try:
            self.fast.purge(jid)
        except NodesError:
            pass
