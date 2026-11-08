#!/usr/bin/env python3
"""Command-line interface for the compute node coordinator admin API.

  python3 -I p1/nodes/ctl.py nodes
  python3 -I p1/nodes/ctl.py enable|disable|stop|rescan <node>
  python3 -I p1/nodes/ctl.py refmc <job> <deck> --histories N [--shards K] [--hist0 H] [--nodes a,b]
  python3 -I p1/nodes/ctl.py openmc <job> <model.xml> --particles P --batches B [--shards K] [--threads T] [--nodes a,b]
  python3 -I p1/nodes/ctl.py jobs | status <job> [--events] | cancel <job> | wait <job>
  python3 -I p1/nodes/ctl.py merge <job> --out result.json [--ref single_run_mctal]
"""
import argparse
import json
import sys
import time
from pathlib import Path

if __package__:
    from . import client as C
    from . import jobs as J
else:
    # When invoked via `python3 -I`, the script directory is omitted from sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import client as C  # noqa: E402
    import jobs as J  # noqa: E402


def gb(x):
    return f'{x / 2 ** 30:.1f}G' if x else '-'


def show_nodes(c):
    for n in c.nodes()['nodes']:
        r = n.get('rate') or {}
        host = n.get('host') or {}
        extra = []
        if host:
            extra.append(f"present={host.get('present')} ecores={host.get('ecores')} ac={host.get('ac')}")
        if n.get('reject'):
            extra.append(n['reject'])
        print(f"{n['name']:10s} {'online ' if n['online'] else 'offline'} {n['status']:8s} enabled={int(n['enabled'])} "
              f"slots {n['used']}/{n['slots']} cpus {n.get('cpus')} mem {gb(n.get('mem_bytes'))} load {n.get('load1')} "
              f"rate {r.get('per_s', 0):.0f} {r.get('unit', '')}/s agent {(n.get('agent') or '')[:12]} proto {n.get('proto')}"
              + (' ' + ' '.join(extra) if extra else ''))


def show_job(j):
    print(j['id'], j['state'], j['by_state'], 'span', f"{j['span_s']:.1f}s" if j.get('span_s') else '-', 'max attempts', j['max_attempts'])
    for n, p in sorted(j['per_node'].items()):
        print(f"  {n:10s} shards {p['shards']:3d}  share {p['share'] * 100:5.1f}%  per-slot {p['rate_per_slot'] or 0:8.0f}/s"
              f"  cpu {p['cpu_s']:8.1f}s  wall {p['wall_s']:8.1f}s")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--url')
    ap.add_argument('--token-file')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('nodes')
    for k in ('enable', 'disable', 'stop', 'rescan'):
        sub.add_parser(k).add_argument('node')
    m = sub.add_parser('refmc')
    m.add_argument('job')
    m.add_argument('deck')
    m.add_argument('--histories', type=int, required=True)
    m.add_argument('--shards', type=int)
    m.add_argument('--hist0', type=int)
    m.add_argument('--nodes')
    o = sub.add_parser('openmc')
    o.add_argument('job')
    o.add_argument('model')
    o.add_argument('--particles', type=int, required=True)
    o.add_argument('--batches', type=int, required=True)
    o.add_argument('--shards', type=int)
    o.add_argument('--threads', type=int)
    o.add_argument('--nodes')
    sub.add_parser('jobs')
    s = sub.add_parser('status')
    s.add_argument('job')
    s.add_argument('--events', action='store_true')
    sub.add_parser('cancel').add_argument('job')
    w = sub.add_parser('wait')
    w.add_argument('job')
    w.add_argument('--timeout', type=float, default=86400)
    g = sub.add_parser('merge')
    g.add_argument('job')
    g.add_argument('--out', required=True)
    g.add_argument('--ref')
    a = ap.parse_args(argv)
    c = C.Client(a.url, a.token_file, timeout=30)
    try:
        if a.cmd == 'nodes':
            show_nodes(c)
        elif a.cmd in ('enable', 'disable'):
            print(c.set_enabled(a.node, a.cmd == 'enable'))
        elif a.cmd == 'stop':
            print(c.stop(a.node))
        elif a.cmd == 'rescan':
            print(c.rescan(a.node))
        elif a.cmd in ('refmc', 'openmc'):
            body = {'id': a.job, 'solver': 'ref_mc' if a.cmd == 'refmc' else 'openmc'}
            if a.cmd == 'refmc':
                body.update(deck=Path(a.deck).read_text(encoding='latin-1'), histories=a.histories)
                if a.hist0:
                    body['hist0'] = a.hist0
            else:
                body.update(model=Path(a.model).read_text(), particles=a.particles, batches=a.batches)
                if a.threads:
                    body['threads'] = a.threads
            if a.shards:
                body['shards'] = a.shards
            if a.nodes:
                body['nodes'] = a.nodes.split(',')
            print(json.dumps(c.create_job(body), ensure_ascii=False))
        elif a.cmd == 'jobs':
            for j in c.jobs()['jobs']:
                print(f"{j['id']:16s} {j['solver']:7s} {j['state']:9s} {j['by_state']}")
        elif a.cmd == 'status':
            j = c.job(a.job, a.events)
            show_job(j)
            for e in j.get('events', []):
                print(f"  {time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['node']} {e['idx']} {e['what']}")
        elif a.cmd == 'cancel':
            print(c.cancel(a.job))
        elif a.cmd == 'wait':
            t0 = time.time()
            while True:
                j = c.job(a.job)
                if j['state'] in ('done', 'failed', 'cancelled') or time.time() - t0 > a.timeout:
                    break
                time.sleep(5)
            show_job(j)
            return 0 if j['state'] == 'done' else 1
        elif a.cmd == 'merge':
            r = c.merge(a.job)
            Path(a.out).write_text(json.dumps(r))
            print(f"{a.job}: {r['shards_used']}/{r['shards']} shards merged -> {a.out}")
            if r['solver'] == 'ref_mc':
                print('histories', r['nps'])
                chi = sorted(r['chi2'].values())
                if chi:
                    print(f'shard consistency chi2/(K-1), median over tallies {chi[len(chi) // 2]:.2f}, range {chi[0]:.2f}-{chi[-1]:.2f}')
                if a.ref:
                    rn, ref = J.read_mctal(Path(a.ref).read_text(encoding='latin-1'))
                    merged = {int(t): [tuple(x) for x in v] for t, v in r['tallies'].items()}
                    cmp = J.compare_mctal(merged, ref)
                    print(f"vs reference ({rn} histories): max rel diff of means {cmp['max_rel_diff']:.2e} at {cmp['at']}, "
                          f"max |dR| {cmp['max_abs_dR']:.4f}")
    except C.NodesError as e:
        print(f'error {e.code}: {e.msg}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
