"""Readback comparison: compares the physics model parsed from the written solver input
field-by-field against the confirmed model, and verifies all binding choices.

Verdicts rely exclusively on readback outputs, never trusting generator line maps.
"""
import copy

from ..domain import composition as C
from ..domain import model as M
from ..domain import nuclides as N
from ..util import same_value


def realize(model, binding):
    """Apply binding element-merging rules to produce a solver-comparable reference model."""
    m = copy.deepcopy(model)
    if binding.get('solver') != 'ref_mc':
        return m
    for mid, b in binding['materials'].items():
        merged = set(b.get('merged') or [])
        if not merged:
            continue
        mat = m['materials'][mid]
        basis, fr = C.expand(mat)
        m['materials'][mid] = {'recipe': 'nuclides', 'label': mat.get('label', ''), 'basis': basis,
                               'nuclides': N.merge_elements(basis, fr, merged)}
    return m


def _rows(confirmed, readback, locations, rel):
    a, b = M.physics_flat(confirmed), M.physics_flat(readback)
    rows = []
    for p in sorted(set(a) | set(b)):
        va, vb = a.get(p), b.get(p)
        if p not in b:
            st = 'missing'
        elif p not in a:
            st = 'extra'
        else:
            st = 'same' if same_value(va, vb, rel=rel) else 'diff'
        rows.append({'path': p, 'confirmed': va, 'readback': vb, 'status': st, 'lines': locations.get(p, [])})
    return rows


def _binding_rows(binding, rb, rel):
    rows = []

    def row(path, want, got, lines=None):
        ok = same_value(want, got, rel=rel) if not isinstance(want, (dict, list)) else want == got
        rows.append({'path': path, 'confirmed': want, 'readback': got, 'status': 'same' if ok else 'diff',
                     'lines': lines or []})

    run, rrun = binding['run'], rb.get('run') or {}
    for k in ('particles', 'batches', 'inactive'):
        row(f'run.{k}', run[k], rrun.get(k))
    if binding['solver'] == 'openmc':
        row('run.mode', run.get('mode', 'eigenvalue'), rrun.get('mode'))
        row('run.seed', run['seed'], rrun.get('seed'))
        row('run.source', 'box_fissionable', rrun.get('source'))
        row('temperature.method', binding['temperature']['method'], rb['temperature']['method'])
        row('temperature.default_K', binding['temperature']['default_K'], rb['temperature']['default_K'])
        for mid, b in binding['materials'].items():
            row(f'materials.{mid}.sab', b.get('sab'), rb['materials'].get(mid, {}).get('sab'))
    else:
        row('run.mode', run.get('mode', 'n'), rrun.get('mode'))
        for mid, b in binding['materials'].items():
            got = rb['materials'].get(mid, {})
            want_tables = dict(b['tables'])
            row(f'materials.{mid}.tables', want_tables, got.get('tables'))
            row(f'materials.{mid}.sab', b.get('sab'), got.get('sab'))
        for rid, t in binding['tmp_K'].items():
            row(f'tmp.{rid}', t, rb['tmp_K'].get(rid))
    return rows


def compare(model, binding, readback, rel=1e-8):
    """Return {status, rows, binding_rows, mismatches, notes, uninterpreted}; status is 'pass' or 'fail'."""
    conf = realize(model, binding)
    rows = _rows(conf, readback['model'], readback.get('locations', {}), rel)
    brows = _binding_rows(binding, readback['binding'], rel)
    bad = [r for r in rows + brows if r['status'] != 'same']
    for item in readback.get('uninterpreted', []):
        name = item['card'] if isinstance(item, dict) else item
        if binding['solver'] == 'ref_mc' and name.lower() in ('print', 'prdmp'):
            continue
        bad.append({'path': f'uninterpreted.{name}', 'confirmed': None, 'readback': name,
                    'status': 'extra', 'lines': [item['line']] if isinstance(item, dict) else []})
    return {'status': 'fail' if bad else 'pass', 'rows': rows, 'binding_rows': brows,
            'mismatches': bad, 'notes': readback.get('notes', []),
            'uninterpreted': readback.get('uninterpreted', [])}


def model_diff(a, b, rel=1e-6):
    """Physics differences between two readback models (only differing fields)."""
    return [r for r in M.diff_models(a, b, rel=rel)]
