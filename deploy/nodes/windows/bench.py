"""Foreground CPU load generator for yield testing: spawns N normal-priority, unthrottled processes
that perform a fixed amount of work and print elapsed times.

Usage: python bench.py 4 250000000
"""
import ctypes
import ctypes.wintypes as W
import multiprocessing as mp
import sys
import time


class THROTTLE(ctypes.Structure):
    _fields_ = [('Version', W.ULONG), ('ControlMask', W.ULONG), ('StateMask', W.ULONG)]


def work(n, q):
    k32 = ctypes.WinDLL('kernel32')
    k32.GetCurrentProcess.restype = W.HANDLE
    k32.SetProcessInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    t = THROTTLE(1, 1, 0)
    k32.SetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(t), ctypes.sizeof(t))
    t0 = time.perf_counter()
    x = 1
    for _ in range(n):
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
    q.put(time.perf_counter() - t0)


if __name__ == '__main__':
    procs, n = int(sys.argv[1]), int(sys.argv[2])
    q = mp.Queue()
    ps = [mp.Process(target=work, args=(n, q)) for _ in range(procs)]
    t0 = time.perf_counter()
    for p in ps:
        p.start()
    times = [q.get() for _ in ps]
    for p in ps:
        p.join()
    print(f'procs={procs} wall={time.perf_counter() - t0:.1f}s per-proc mean={sum(times) / len(times):.1f}s max={max(times):.1f}s')
