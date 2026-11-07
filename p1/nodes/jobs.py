"""Job sharding, result merging, and whole-case workload/data dependency discovery.

Reference MC: splits fixed-source jobs into disjoint contiguous history ranges using `NPS n_k` and
`RAND GEN=2 HIST=h_k`.
OpenMC: assigns each shard a deterministic seed 4 * (sha256(job/idx) mod 2^40) + 1 (preserving a
4-stream stride so adjacent shards never share overlapping PRNG substreams).
Merging consumes a contiguous prefix from shard 0 so results are independent of node completion order.
Whole-case criticality jobs run unsharded (single shard, unmodified input). Uses stdlib only; openmc
is imported solely when merging OpenMC tallies.
"""
import hashlib
import math
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CONTROL = re.compile(r'^\s{0,4}(nps|ctme|rand|prdmp)\b', re.I)
KCODE = re.compile(r'^\s{0,4}kcode\b(.*)$', re.I)
REF_MC_ARGS = ['i=inp', 'o=outp', 'r=rtp', 'm=mctl']
REF_MC_OUTPUTS = ['mctl', 'outp']
OPENMC_OUTPUTS = ['statepoint.*.h5']
# Whole-case runs use the same filenames as local runs so the kernel parses results identically
CASE_REF_MC_ARGS = ['i=case.i', 'o=case.o', 'r=case.r', 'm=case.m']
CASE_REF_MC_OUTPUTS = ['case.o', 'run.log']
# Disable Fortran block buffering so KCODE cycle progress flushes to run.log immediately
CASE_REF_MC_ENV = {'GFORTRAN_UNBUFFERED_PRECONNECTED': 'y'}
CASE_OPENMC_ARGS = ['-s', '{threads}', '{restart}', '.']
CASE_OPENMC_OUTPUTS = ['statepoint.*.h5', 'run.log']
STATEPOINT = re.compile(r'^statepoint\.\d+\.h5$')
_SHA_CACHE = {}
# GEN=2 is a 63-bit LCG with stride 152917: usable history range is ~6.0e13
HIST_LIMIT = (1 << 63) // 152917


def sha(p):
    """Cached sha256 keyed by (path, size, mtime_ns) for multi-GB nuclear data libraries."""
    st = os.stat(p)
    key = (str(p), st.st_size, st.st_mtime_ns)
    if key in _SHA_CACHE:
        return _SHA_CACHE[key]
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    _SHA_CACHE[key] = h.hexdigest()
    return _SHA_CACHE[key]


def ident(paths):
    """Compute executable identity hash by chaining sha256 hex digests, matching the worker agent."""
    return hashlib.sha256(''.join(sha(Path(p).expanduser()) for p in paths).encode()).hexdigest()


def split_counts(total, k):
    """Split total into k integers, giving +1 to the first (total % k) parts."""
    q, r = divmod(total, k)
    return [q + (1 if i < r else 0) for i in range(k)]


def ref_mc_base(deck_text):
    """Strip NPS, CTME, RAND, PRDMP control cards and trailing blank lines while keeping deck verbatim."""
    lines = deck_text.replace('\r\n', '\n').rstrip('\n').split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    return [l for l in lines if not CONTROL.match(l)]


def ref_mc_shard_text(base, nps, hist):
    return '\n'.join(base + ['prdmp j j 1 2 j', f'nps {nps}', f'rand gen=2 hist={hist}']) + '\n'


def ref_mc_probe(base, datapath, exe, timeout=600):
    """Discover actual data files opened by running 1 history under strace; card parsing misses implicit reads."""
    datapath = Path(datapath).expanduser()
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'inp').write_text('\n'.join(base + ['nps 1']) + '\n', encoding='latin-1')
        subprocess.run(['strace', '-f', '-e', 'trace=openat', '-o', 'trace.txt', str(Path(exe).expanduser())] + REF_MC_ARGS,
                       cwd=d, env={'DATAPATH': str(datapath), 'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=timeout)
        opened = set()
        for l in Path(d, 'trace.txt').read_text(errors='replace').splitlines():
            m = re.search(r'openat\([^"]*"([^"]+)"', l)
            if m and 'ENOENT' not in l and m.group(1).startswith(str(datapath) + '/'):
                opened.add(m.group(1)[len(str(datapath)) + 1:])
    if 'xsdir' not in opened:
        raise RuntimeError('Reference MC did not open xsdir during 1-history probe; check input deck and data_root')
    return {f: sha((datapath / f).resolve()) for f in sorted(opened)}


def ref_mc_kcode(deck_text):
    """Return (particles_per_cycle, total_cycles) from KCODE card, or None if absent (skipping title line)."""
    for l in deck_text.replace('\r\n', '\n').split('\n')[1:]:
        m = KCODE.match(l)
        if m:
            f = m.group(1).split()
            return int(float(f[0])), int(float(f[3]))
    return None


def ref_mc_case_probe_base(deck_text):
    """Build a minimal 20-particle, 2-cycle probe deck to record all cross-section files opened before transport."""
    base = ref_mc_base(deck_text)
    return base[:1] + ['kcode 20 1.0 1 2' if KCODE.match(l) else l for l in base[1:]]


def openmc_work(model_text):
    """Total OpenMC particle histories (particles * batches)."""
    st = ET.fromstring(model_text).find('settings')
    if st is None or st.find('particles') is None or st.find('batches') is None:
        raise ValueError('Model <settings> must define particles and batches')
    return int(st.find('particles').text) * int(st.find('batches').text)


def openmc_batches(model_text):
    """Total batch count from OpenMC model.xml."""
    return int(ET.fromstring(model_text).find('settings').find('batches').text)


def files_size(root, files):
    return sum((Path(root).expanduser() / f).resolve().stat().st_size for f in files)


def openmc_seed(job, k):
    return 4 * (int(hashlib.sha256(f'{job}/{k}'.encode()).hexdigest(), 16) % (1 << 40)) + 1


def openmc_files(model_text, data_root):
    """Resolve required nuclide and S(alpha,beta) HDF5 paths and sha256 digests from cross_sections.xml."""
    root = Path(data_root).expanduser()
    lib = ET.parse(root / 'cross_sections.xml').getroot()
    path_of = {lb.get('materials'): lb.get('path') for lb in lib.iter('library')}
    tree = ET.fromstring(model_text)
    need = set()
    for m in tree.iter('material'):
        need |= {n.get('name') for n in m.iter('nuclide')} | {n.get('name') for n in m.iter('sab')}
    missing = sorted(n for n in need if n not in path_of)
    if missing:
        raise ValueError('Missing from cross_sections.xml: ' + ', '.join(missing))
    return {path_of[n]: sha((root / path_of[n]).resolve()) for n in sorted(need)}


def openmc_shard_xml(model_text, particles, batches, seed):
    t = ET.ElementTree(ET.fromstring(model_text))
    st = t.getroot().find('settings')
    if st is None:
        raise ValueError('Model missing <settings> element')
    for tag, val in (('particles', particles), ('batches', batches), ('seed', seed)):
        el = st.find(tag)
        if el is None:
            el = ET.SubElement(st, tag)
        el.text = str(val)
    with tempfile.NamedTemporaryFile(suffix='.xml') as f:
        t.write(f.name, encoding='utf-8', xml_declaration=True)
        return Path(f.name).read_bytes()


# ---------- Merging ----------

def read_mctal(text):
    """Parse mctal total histories and per-tally bin (mean, relative_error) tuples."""
    nps = int(text.split('\n')[0].split()[5])
    out = {}
    for m in re.finditer(r'^tally\s+(\d+).*?^vals\n(.*?)^tfc', text, re.S | re.M):
        nums = m.group(2).split()
        out[int(m.group(1))] = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums), 2)]
    return nps, out


def merge_mctal(parts):
    """Merge ordered [(N_k, {tally: [(xbar_k, R_k)]})] shards.

    S1_k = N_k * xbar_k, S2_k = S1_k^2 * (R_k^2 + 1/N_k);
    xbar = sum(S1) / sum(N), R^2 = sum(S2) / sum(S1)^2 - 1 / sum(N).
    chi2 is the per-tally median of sum_k N_k(xbar_k - xbar)^2 / (sum(N) * (R * xbar)^2) / (K - 1),
    using merged variance so low-count bins with correlated R_k do not inflate chi2.
    """
    N = sum(n for n, _ in parts)
    res, chi = {}, {}
    K = len(parts)
    for t in parts[0][1]:
        nb = len(parts[0][1][t])
        for n, d in parts:
            if len(d.get(t, ())) != nb:
                raise ValueError(f'Tally {t} bin count differs across shards')
        bins, cs = [], []
        for b in range(nb):
            S = Q = 0.0
            for n, d in parts:
                m, r = d[t][b]
                s1 = m * n
                S += s1
                Q += s1 * s1 * (r * r + 1.0 / n)
            mean = S / N
            R = math.sqrt(max(Q / (S * S) - 1.0 / N, 0.0)) if S else 0.0
            bins.append((mean, R))
            if K > 1 and mean and R:
                cs.append(sum(n * (d[t][b][0] - mean) ** 2 for n, d in parts) / (N * (R * mean) ** 2) / (K - 1))
        res[t] = bins
        if cs:
            cs.sort()
            chi[t] = cs[len(cs) // 2]
    return N, res, chi


def compare_mctal(merged, ref):
    """Compare merged tally bins against a single-run reference: max relative mean diff and max |dR|."""
    worst = worst_r = 0.0
    where = None
    for t, rb in ref.items():
        for b, ((m, r), (rm, rr)) in enumerate(zip(merged[t], rb)):
            if rm and abs(m / rm - 1) > worst:
                worst, where = abs(m / rm - 1), (t, b)
            worst_r = max(worst_r, abs(r - rr))
    return {'max_rel_diff': worst, 'at': where, 'max_abs_dR': worst_r}


def merge_openmc(paths):
    """Merge OpenMC statepoints via batch sums: sum, sum_sq, realizations, and inter-shard chi2 median."""
    import openmc
    acc = {}
    for p in paths:
        with openmc.StatePoint(p) as sp:
            for tid, t in sp.tallies.items():
                s = acc.setdefault(tid, [0.0, 0.0, 0, []])
                s[0] = s[0] + t.sum
                s[1] = s[1] + t.sum_sq
                s[2] += t.num_realizations
                s[3].append(t.mean.ravel())
    out = {}
    for tid, (S, Q, n, means) in acc.items():
        mean = (S / n).ravel()
        sd = (((Q / n).ravel() - mean ** 2) / (n - 1)) ** 0.5
        k = len(means)
        # With equal batches per shard, shard mean variance equals k * merged mean variance
        chi = sorted(sum((m[i] - mean[i]) ** 2 for m in means) / (sd[i] ** 2 * k) / (k - 1)
                     for i in range(len(mean)) if sd[i] > 0) if k > 1 else []
        out[str(tid)] = {'mean': mean.tolist(), 'sd': sd.tolist(), 'realizations': n,
                         'chi2': chi[len(chi) // 2] if chi else None}
    return out
