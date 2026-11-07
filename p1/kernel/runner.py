"""Local solver runner: spawns solver processes, reads progress, stops/confirms, resumes from statepoints,
and parses final results.

The kernel interacts solely through the Runner interface. On Linux, OpenMC runs inside a systemd user
scope (`systemd-run --user --scope`) with MemoryMax enforcement so solver runs survive kernel restarts
and reattach via PID and working directory.
"""
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import time

BATCH_LINE = re.compile(r'^\s+(\d+)/(\d+)\s+([0-9.]+)(?:\s+([0-9.]+)\s+\+/-\s+([0-9.]+))?\s*$')
IS_POSIX = os.name == 'posix'


def host_resources(path):
    """Available host memory and free disk space at path (in GB); memory is None if unreadable."""
    mem = None
    try:
        with open('/proc/meminfo', encoding='ascii') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    mem = int(line.split()[1]) / 1048576
                    break
    except OSError:
        pass
    return {'mem_available_gb': mem, 'disk_free_gb': shutil.disk_usage(path).free / 1073741824}


def keep_last_statepoint(run_dir):
    """Prune intermediate statepoints after completion; only the latest is needed for results and restarts."""
    last = latest_statepoint(run_dir)
    freed = 0
    if not last:
        return freed
    for p in glob.glob(os.path.join(run_dir, 'statepoint.*.h5')):
        if p != last[1]:
            freed += os.path.getsize(p)
            os.remove(p)
    return freed


class Runner:
    solver = None

    def version(self):
        return '0.15.3'

    def library_label(self, indexed):
        return indexed

    def available(self):
        raise NotImplementedError

    def start(self, run_dir, threads=4, restart_from=None):
        raise NotImplementedError

    def alive(self, pid, run_dir):
        raise NotImplementedError

    def stop(self, pid, timeout=10.0):
        raise NotImplementedError


class OpenmcLocal(Runner):
    solver = 'openmc'

    def __init__(self, memory_max='3G', cross_sections=None):
        self.memory_max = memory_max
        self.cross_sections = cross_sections or os.environ.get('OPENMC_CROSS_SECTIONS')
        self.exe = shutil.which('openmc') or os.path.join(os.path.dirname(sys.executable), 'openmc')
        self._version = None

    def available(self):
        return os.path.exists(self.exe) and bool(self.cross_sections) and os.path.exists(self.cross_sections)

    def _cmd(self, args, scope):
        cmd = [self.exe] + args
        if scope and IS_POSIX and shutil.which('systemd-run'):
            cmd = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={self.memory_max}',
                   '--unit', scope] + cmd
        return cmd

    def start(self, run_dir, threads=4, restart_from=None, log='run.log', scope=None):
        args = ['-s', str(threads)]
        if restart_from:
            args += ['-r', os.path.basename(restart_from)]
        env = dict(os.environ, OPENMC_CROSS_SECTIONS=self.cross_sections)
        out = open(os.path.join(run_dir, log), 'ab')
        p = subprocess.Popen(self._cmd(args, scope), cwd=run_dir, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, start_new_session=IS_POSIX)
        out.close()
        return p

    def version(self):
        """Actual solver binary version, falling back to generator target version if unavailable."""
        if self._version is None:
            try:
                out = subprocess.run([self.exe, '--version'], capture_output=True, text=True, timeout=20).stdout
                m = re.search(r'(\d+\.\d+\.\d+)', out)
                self._version = m.group(1) if m else '0.15.3'
            except (OSError, subprocess.SubprocessError):
                self._version = '0.15.3'
        return self._version

    def library_label(self, indexed):
        """Nuclear data library label; prevents run cache reuse when cross_sections.xml changes."""
        default = os.environ.get('P1_LIBRARY_LABEL')
        return default or indexed

    def run_sync(self, run_dir, args, log, timeout=600, scope=None):
        env = dict(os.environ, OPENMC_CROSS_SECTIONS=self.cross_sections)
        with open(os.path.join(run_dir, log), 'wb') as out:
            t0 = time.time()
            rc = subprocess.call(self._cmd(args, scope), cwd=run_dir, stdout=out, stderr=subprocess.STDOUT,
                                 env=env, timeout=timeout)
        with open(os.path.join(run_dir, log), encoding='utf-8', errors='replace') as f:
            return rc, f.read(), time.time() - t0

    def alive(self, pid, run_dir):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode(errors='replace')
            cwd = os.readlink(f'/proc/{pid}/cwd')
        except OSError:
            return False
        return 'openmc' in cmd and os.path.realpath(cwd) == os.path.realpath(run_dir)

    def stop(self, pid, run_dir, timeout=10.0):
        """Returns 'stopped' or 'stop_unconfirmed'."""
        if not self.alive(pid, run_dir):
            return 'stopped'
        for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(pid, sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            t0 = time.time()
            while time.time() - t0 < wait:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                if not self.alive(pid, run_dir):
                    return 'stopped'
                time.sleep(0.2)
        return 'stop_unconfirmed'


class RefMcLocal(OpenmcLocal):
    """Serial Reference MC runner: tools/ref_mc loads deploy/ref_mc.env for executable and data paths;
    filenames are limited to 8 characters and each run uses a fresh directory."""
    solver = 'ref_mc'
    NAME = 'ref_mc'

    def __init__(self, memory_max='3G'):
        self.memory_max = memory_max
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.exe = os.path.join(base, 'tools', 'ref_mc')
        self.env_file = os.path.join(base, 'deploy', 'ref_mc.env')
        self._version = '1.14'

    def available(self):
        if not (os.path.exists(self.exe) and os.path.exists(self.env_file)):
            return False
        with open(self.env_file, encoding='utf-8') as f:
            for line in f:
                if line.startswith('REF_MC_EXEC='):
                    return os.path.exists(line.split('=', 1)[1].strip().strip('"\''))
        return False

    def start(self, run_dir, scope=None, log='run.log'):
        out = open(os.path.join(run_dir, log), 'ab')
        # Disable Fortran block buffering so cycle lines flush to run.log in real time
        env = dict(os.environ, GFORTRAN_UNBUFFERED_PRECONNECTED='y')
        p = subprocess.Popen(self._cmd(['i=case.i', 'o=case.o', 'r=case.r', 'm=case.m'], scope), cwd=run_dir, stdout=out,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=IS_POSIX, env=env)
        out.close()
        return p

    def alive(self, pid, run_dir):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode(errors='replace')
            cwd = os.readlink(f'/proc/{pid}/cwd')
        except OSError:
            return False
        return 'ref_mc' in cmd and os.path.realpath(cwd) == os.path.realpath(run_dir)


KEFF_FINAL = re.compile(r'final estimated combined collision/absorption/track-length keff\s*=\s*([0-9.]+)\s+'
                        r'with an estimated standard deviation of\s*([0-9.]+)', re.S)
REF_MC_TERM = re.compile(r'run terminated when\s+(\d+)\s+kcode cycles were done')
REF_MC_NPS = re.compile(r'dump\s+\d+\s+on file\s+\S+\s+nps\s*=\s*(\d+)')
REF_MC_LOST = re.compile(r'(\d+)\s+particles? got lost')
REF_MC_FATAL = re.compile(r'^\s*(?:fatal error|bad trouble).*$', re.I | re.M)


def read_ref_mc_output(run_dir):
    """Parse case.o for termination reason, completed cycles, and final k_eff (even when exit code is 0)."""
    path = os.path.join(run_dir, 'case.o')
    if not os.path.exists(path):
        return {'ok': False, 'why': 'Missing output file case.o'}
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    fatal = REF_MC_FATAL.findall(text)
    if fatal:
        return {'ok': False, 'why': fatal[0].strip()[:160]}
    term = REF_MC_TERM.search(text)
    m = KEFF_FINAL.search(text)
    if not term:
        return {'ok': False, 'why': 'No normal termination record found in case.o'}
    if not m:
        return {'ok': False, 'why': f'Completed {term.group(1)} cycles without final keff (requires >= 30 active cycles)'}
    lost = REF_MC_LOST.search(text)
    nps = REF_MC_NPS.findall(text)
    return {'ok': True, 'k': float(m.group(1)), 'sigma': float(m.group(2)), 'cycles': int(term.group(1)),
            'histories': int(nps[-1]) if nps else None, 'lost': int(lost.group(1)) if lost else 0}


def parse_progress(text):
    """Return {batch, k_batch, k, sigma} from OpenMC stdout lines."""
    last = None
    for line in text.splitlines():
        m = BATCH_LINE.match(line)
        if m:
            last = m
    if not last:
        return None
    g = last.groups()
    return {'batch': int(g[0]), 'k_batch': float(g[2]),
            'k': float(g[3]) if g[3] else None, 'sigma': float(g[4]) if g[4] else None}


REF_MC_CYCLE = re.compile(r'^\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)(?:\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)\s+\d+)?\s*$')


def parse_ref_mc_progress(text):
    """Parse last KCODE cycle line from Reference MC stdout: completed cycles, cycle k, and cumulative k/sigma."""
    last = None
    for line in text.splitlines():
        m = REF_MC_CYCLE.match(line)
        if m:
            last = m
    if not last:
        return None
    g = last.groups()
    return {'cycle': int(g[0]), 'k_cycle': float(g[1]), 'k': float(g[4]) if g[4] else None,
            'sigma': float(g[5]) if g[5] else None}


def latest_statepoint(run_dir):
    sps = []
    for p in glob.glob(os.path.join(run_dir, 'statepoint.*.h5')):
        m = re.search(r'statepoint\.(\d+)\.h5$', p)
        if m:
            sps.append((int(m.group(1)), p))
    return max(sps) if sps else None


def read_result(statepoint):
    import openmc
    with openmc.StatePoint(statepoint) as sp:
        k = sp.keff
        out = {'k': float(k.nominal_value), 'sigma': float(k.std_dev),
               'estimator': 'combined (collision, track-length, absorption)',
               'batches': int(sp.n_batches), 'inactive': int(sp.n_inactive),
               'particles': int(sp.n_particles), 'version': '.'.join(map(str, sp.version)),
               'per_estimator': {}}
        try:
            names = ('collision', 'track-length', 'absorption')
            g = sp.global_tallies
            for i, n in enumerate(names):
                row = g[i]
                out['per_estimator'][n] = {'k': float(row['mean']), 'sigma': float(row['std_dev'])}
        except Exception:
            pass
    out['rel_err'] = out['sigma'] / out['k'] if out['k'] else None
    return out
