"""Windows host-side daemon for the WSL2 compute node: keeps the worker agent running headless,
dynamically throttles vmmemWSL by whole-host CPU load, and shares host telemetry with the agent.

vmmemWSL always runs at IDLE_PRIORITY_CLASS. Yield decisions live in yield_policy.py: full speed
by default; pins to efficiency cores with EcoQoS when total CPU stays >90%; gradually suspends
shards near saturation; suspends all shards on battery power. User presence is reported as status only.
"""
import ctypes
import ctypes.wintypes as W
import json
import subprocess
import sys
import time
from pathlib import Path

# Add script dir explicitly so pythonw or python -I can import sibling yield_policy
sys.path.insert(0, str(Path(__file__).resolve().parent))
import yield_policy  # noqa: E402

DISTRO = 'pmx-node'
BASE = Path(__file__).resolve().parent
LOG = BASE / 'controller.log'
PAUSE = Path(rf'\\wsl.localhost\{DISTRO}\opt\promptmc\pmx-node\work\PAUSE')
HOST = Path(rf'\\wsl.localhost\{DISTRO}\opt\promptmc\pmx-node\work\host.json')
AGENT_STATE = Path(rf'\\wsl.localhost\{DISTRO}\opt\promptmc\pmx-node\work\agent.json')
HOST_EVERY_S = 30       # Rewrite host.json periodically even when unchanged so agent tracks freshness
PAUSE_EVERY_S = 30      # Rewrite PAUSE periodically: agent ignores PAUSE older than 120 s on controller exit
RUNNING_EVERY_S = 30    # Poll agent.json active slot count interval in efficiency mode
AGENT = ['wsl.exe', '-d', DISTRO, '-u', 'promptmc', '--', '/opt/promptmc/miniforge3/envs/openmc/bin/python', '-I',
         '/opt/promptmc/pmx-node/agent.py', '/opt/promptmc/pmx-node/node.json']
AWAY_S = 300            # Idle input threshold (ms-level) for reporting user presence
STATUS_EVERY_S = 300
CREATE_NO_WINDOW = 0x08000000
IDLE_PRIORITY_CLASS = 0x40

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
u32 = ctypes.WinDLL('user32', use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.CreateToolhelp32Snapshot.restype = W.HANDLE
k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [W.HANDLE, ctypes.c_void_p]
k32.CloseHandle.argtypes = [W.HANDLE]
k32.SetPriorityClass.argtypes = [W.HANDLE, W.DWORD]
k32.SetProcessInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
k32.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.c_void_p] * 4
k32.GetSystemCpuSetInformation.argtypes = [ctypes.c_void_p, W.ULONG, ctypes.POINTER(W.ULONG), W.HANDLE, W.ULONG]
k32.SetProcessDefaultCpuSets.argtypes = [W.HANDLE, ctypes.c_void_p, W.ULONG]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
                ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', W.LONG),
                ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', W.UINT), ('dwTime', W.DWORD)]


class POWER(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_byte), ('BatteryFlag', ctypes.c_byte), ('BatteryLifePercent', ctypes.c_byte),
                ('SystemStatusFlag', ctypes.c_byte), ('BatteryLifeTime', W.DWORD), ('BatteryFullLifeTime', W.DWORD)]


class THROTTLE(ctypes.Structure):
    _fields_ = [('Version', W.ULONG), ('ControlMask', W.ULONG), ('StateMask', W.ULONG)]


def log(*a):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a) + '\n')
    if LOG.stat().st_size > 2_000_000:
        LOG.replace(LOG.with_suffix('.log.1'))


def pids(name):
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    out = []
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        if e.szExeFile.lower() == name.lower():
            out.append(e.th32ProcessID)
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


def ft(x):
    return (x.dwHighDateTime << 32 | x.dwLowDateTime) / 1e7


def vm_cpu_seconds(handles):
    total = 0.0
    for h in handles:
        c, e, kt, ut = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
        if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            total += ft(kt) + ft(ut)
    return total


def system_busy_seconds():
    i, kt, ut = W.FILETIME(), W.FILETIME(), W.FILETIME()
    k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(kt), ctypes.byref(ut))
    return ft(kt) + ft(ut) - ft(i)


def idle_seconds():
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    u32.GetLastInputInfo(ctypes.byref(li))
    return (k32.GetTickCount() - li.dwTime) / 1000.0


def on_ac():
    p = POWER()
    k32.GetSystemPowerStatus(ctypes.byref(p))
    return p.ACLineStatus != 0


def efficiency_cpu_sets():
    """Return CPU set IDs with the lowest efficiency class (E-cores on hybrid architectures)."""
    n = W.ULONG()
    k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(n), None, 0)
    buf = ctypes.create_string_buffer(n.value)
    k32.GetSystemCpuSetInformation(buf, n, ctypes.byref(n), None, 0)
    rows, off = [], 0
    while off < n.value:
        size = int.from_bytes(buf.raw[off:off + 4], 'little')
        rows.append((int.from_bytes(buf.raw[off + 8:off + 12], 'little'), buf.raw[off + 18]))
        off += size
    low = min(e for _, e in rows)
    return [i for i, e in rows if e == low] if len({e for _, e in rows}) > 1 else []


ECORES = []


def set_vm(handles, eco):
    """Full speed: idle priority, all cores, no throttling. Eco mode: idle priority, EcoQoS, E-cores only."""
    for h in handles:
        k32.SetPriorityClass(h, IDLE_PRIORITY_CLASS)
        t = THROTTLE(1, 1, 1 if eco else 0)
        k32.SetProcessInformation(h, 4, ctypes.byref(t), ctypes.sizeof(t))
        if eco and ECORES:
            ids = (W.ULONG * len(ECORES))(*ECORES)
            k32.SetProcessDefaultCpuSets(h, ids, len(ECORES))
        else:
            k32.SetProcessDefaultCpuSets(h, None, 0)


def write_host(state):
    try:
        HOST.write_text(json.dumps(state))
        return True
    except OSError as e:
        log('host status write failed', repr(e))
        return False


def read_running():
    """Read active slot count and per-level resume slot map reported by the worker agent."""
    try:
        state = json.loads(AGENT_STATE.read_text())
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def write_pause(state):
    """Write atomically via temp file + rename so the agent never observes a partial PAUSE file."""
    try:
        if state:
            tmp = PAUSE.with_name('PAUSE.tmp')
            tmp.write_text(state + '\n')
            tmp.replace(PAUSE)
        elif PAUSE.exists():
            PAUSE.unlink()
        return True
    except OSError as e:
        log('pause write failed', repr(e))
        return False


class LUID(ctypes.Structure):
    _fields_ = [('LowPart', W.DWORD), ('HighPart', W.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Luid', LUID), ('Attributes', W.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [('PrivilegeCount', W.DWORD), ('Privileges', LUID_AND_ATTRIBUTES * 1)]


def enable_debug_privilege():
    """vmmemWSL is a system-owned process requiring SeDebugPrivilege; scheduled admin tokens carry it disabled by default."""
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
    k32.GetCurrentProcess.restype = W.HANDLE
    tok = W.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(tok)):
        return ctypes.get_last_error()
    luid = LUID()
    if not adv.LookupPrivilegeValueW(None, 'SeDebugPrivilege', ctypes.byref(luid)):
        return ctypes.get_last_error()
    tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, 2)))
    ok = adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
    err = ctypes.get_last_error() if ok else (ctypes.get_last_error() or -1)
    k32.CloseHandle(tok)
    return err


class Controller:
    def __init__(self):
        self.agent, self.next_start = None, 0.0
        self.handles, self.handle_pids = [], []
        self.ncpu = k32.GetActiveProcessorCount(0xFFFF)
        self.last_sys, self.last_vm, self.last_t = system_busy_seconds(), 0.0, time.time()
        self.warm = 2               # Skip first two ticks after vmmemWSL PID changes (incomplete deltas)
        self.policy = yield_policy.Policy(ncpu=self.ncpu)
        # None forces an initial PAUSE write/clear on the first tick
        self.mode, self.pause, self.next_pause = 'full', None, 0.0
        self.applied, self.next_apply = None, 0.0
        self.running = None         # Most recently polled active slot count
        self.running_t = 0.0
        self.next_status = 0.0
        self.last_host, self.next_host, self.host_retry = None, 0.0, 0.0

    def read_running(self):
        """Called every tick in eco mode, throttled to RUNNING_EVERY_S: reading \\wsl.localhost crosses the VM
        boundary when foreground processes compete for CPU; falls back to cached value on failure."""
        now = time.time()
        if now - self.running_t >= RUNNING_EVERY_S:
            self.running_t = now
            r = read_running()
            self.running = r if r is not None else self.running
        return self.running

    def keep_agent(self, now):
        if self.agent is None or self.agent.poll() is not None:
            if self.agent is not None:
                log('agent exited rc', self.agent.returncode)
                self.agent = None
                self.next_start = now + 60
            if now >= self.next_start:
                out = open(BASE / 'agent.log', 'ab')
                self.agent = subprocess.Popen(AGENT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                              creationflags=CREATE_NO_WINDOW)
                log('agent started pid', self.agent.pid)

    def keep_handles(self, now):
        vm = pids('vmmemWSL')
        if vm == self.handle_pids:
            return
        for h in self.handles:
            k32.CloseHandle(h)
        self.handles = []
        for p in vm:
            h = k32.OpenProcess(0x1000 | 0x0200 | 0x2000, False, p)
            if h:
                self.handles.append(h)
            else:
                log('OpenProcess failed', p, ctypes.get_last_error())
        self.handle_pids, self.applied, self.warm = vm, None, 2
        self.last_sys, self.last_vm, self.last_t = system_busy_seconds(), vm_cpu_seconds(self.handles), now
        log('vmmemWSL', vm, 'handles', len(self.handles))

    def step(self):
        now = time.time()
        self.keep_agent(now)
        self.keep_handles(now)
        sys_now, vm_now = system_busy_seconds(), vm_cpu_seconds(self.handles)
        span = max(now - self.last_t, 1e-3) * self.ncpu
        total = min(1.0, max(0.0, sys_now - self.last_sys) / span)
        wsl = min(total, max(0.0, vm_now - self.last_vm) / span)
        self.last_sys, self.last_vm, self.last_t = sys_now, vm_now, now
        away = idle_seconds() >= AWAY_S
        ac = on_ac()
        P = self.policy
        if self.warm:
            self.warm -= 1
        else:
            mode, want = P.step(now, total, wsl, ac, self.read_running)
            if mode != self.mode:
                log('mode ->', mode, f'total={total:.2f} other={total - wsl:.2f} wsl={wsl:.2f} slots={P.slots} '
                    f'projected={P.projected:.2f}')
                self.mode = mode
            if want != self.pause or now >= self.next_pause:
                if write_pause(want):
                    if want != self.pause:
                        log('pause ->', want or 'none', f'level={P.level} r0={P.r0} total={total:.2f} other={total - wsl:.2f} ac={ac}')
                    self.pause, self.next_pause = want, now + PAUSE_EVERY_S
        if now >= self.next_status:
            log('status', f'total={total:.2f} other={total - wsl:.2f} wsl={wsl:.2f} mode={self.mode} level={P.level} '
                f'pause={self.pause or "none"} away={away} ac={ac}')
            self.next_status = now + STATUS_EVERY_S
        eco = self.mode == 'eco'
        if self.handles and (eco != self.applied or now >= self.next_apply):
            set_vm(self.handles, eco)
            if eco != self.applied:
                log('vm priority idle,', f'CPU busy: EcoQoS on, E-core cpu sets {ECORES}' if eco else 'all cores, full speed')
            self.applied, self.next_apply = eco, now + 60
        host = {'present': not away, 'ecores': bool(eco and ECORES and self.handles), 'ecoqos': eco, 'ac': ac, 'pause': self.pause or ''}
        if (host != self.last_host or now >= self.next_host) and now >= self.host_retry:
            ok = write_host(dict(host, total=round(total, 3), other=round(total - wsl, 3)))
            # Back off 60 s if WSL is down so we do not spam the log every 5 s
            self.last_host = host if ok else None
            self.next_host = now + HOST_EVERY_S
            self.host_retry = now if ok else now + 60


def main():
    mutex = k32.CreateMutexW(None, False, 'Local\\pmx-node-controller')
    if ctypes.get_last_error() == 183:
        return
    ECORES[:] = efficiency_cpu_sets()
    err = enable_debug_privilege()
    log('controller start', sys.executable, 'SeDebugPrivilege', 'enabled' if err == 0 else f'not enabled ({err})')
    if err:
        for args in (['whoami', '/groups'], ['whoami', '/priv']):
            r = subprocess.run(args, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            log(' '.join(args), '|'.join(l.split()[0] + ' ' + l.split()[-1] for l in r.stdout.splitlines() if 'Mandatory' in l or 'Se' in l[:3]))
    try:
        subprocess.run(['wsl.exe', '-d', DISTRO, '-u', 'promptmc', '--', 'pkill', '-f', 'pmx-node/agent.py'],
                       stdin=subprocess.DEVNULL, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=120)
    except subprocess.TimeoutExpired:
        log('stopping the old agent timed out')
    ctl = Controller()
    while True:
        try:
            ctl.step()
        except Exception:
            import traceback
            log('error', traceback.format_exc())
        time.sleep(5)


if __name__ == '__main__':
    main()
