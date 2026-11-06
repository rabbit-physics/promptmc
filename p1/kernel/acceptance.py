"""Reproduction acceptance evaluation and discrepancy investigation.

Tolerances are locked when confirming the goal: |Delta| <= k * sigma_c passes, where
sigma_c = (sigma^2 + sigma_ref^2)^(1/2). Known systematics are listed separately without altering
the verdict when unquantified; observed discrepancies never relax tolerances. Out-of-tolerance
results trigger investigation across alternative readings, or mark not_reproducible for user action.
"""
import json
import math
import os

from .. import binding as B
from ..domain import params as P

HERE = os.path.dirname(os.path.dirname(__file__))


def sensitivity_table(plist=None):
    """Misreading sensitivity table is available only for covered benchmark problems."""
    if plist is not None and plist.get('problem') != 'VERA 1A':
        return {'rows': [], 'source': ''}
    with open(os.path.join(HERE, 'domain', 'vera_sensitivity.json'), encoding='utf-8') as f:
        return json.load(f)


def default_spec(plist, k_sigma=3.0, case='S5'):
    ref = plist['reference']
    library = B.openmc_library()['label']
    code = ref.get('code') or ref['method'].split(',')[0]
    systematics = [{'id': 'code', 'what': f'Solver: OpenMC 0.15.3 vs. reference {code}',
                    'quantified': False, 'value_pcm': None, 'sigma_pcm': None}]
    if ref.get('library') and ref['library'] != library:
        systematics.insert(0, {'id': 'library', 'what': f"Nuclear data: {library} vs. reference {ref['library']}",
                               'quantified': False, 'value_pcm': None, 'sigma_pcm': None})
    if not ref.get('library'):
        systematics.insert(0, {'id': 'library', 'what': f'Nuclear data: {library} vs. unspecified reference library',
                               'quantified': False, 'value_pcm': None, 'sigma_pcm': None})
    return {'type': 'reproduce_reference', 'metric': 'k_eff', 'case': case,
            'reference': {'value': ref['value'], 'sigma': ref['sigma'], 'source': ref['source'],
                          'method': ref['method']},
            'k_sigma': k_sigma, 'known_systematics': systematics}


def pending_spec(case):
    """Placeholder acceptance spec prior to parameter list confirmation."""
    return {'type': 'pending', 'metric': 'k_eff', 'case': case, 'reference': None, 'k_sigma': 3.0, 'known_systematics': []}


def no_reference_spec(case):
    return {'type': 'none', 'metric': 'k_eff', 'case': case, 'reference': None, 'k_sigma': 3.0, 'known_systematics': []}


def evaluate(spec, k, sigma):
    ref = spec['reference']
    if ref is None:
        return {'outcome': 'not_applicable', 'k': k, 'sigma': sigma, 'k_ref': None, 'sigma_ref': None, 'delta_pcm': None,
                'sigma_c_pcm': None, 'z': None, 'k_sigma': spec['k_sigma'],
                'note': 'No reference solution extracted from source; acceptance skipped', 'systematics': []}
    d = k - ref['value']
    sc = math.hypot(sigma, ref['sigma'])
    z = d / sc if sc else float('inf')
    ks = spec['k_sigma']
    if abs(z) <= ks:
        outcome = 'match'
        note = f'|Delta| = {abs(d) * 1e5:.0f} pcm <= {ks:g} sigma_c = {ks * sc * 1e5:.0f} pcm'
        if abs(z) > 2:
            note += '; between 2 sigma and 3 sigma, consider increasing particles to recheck'
    else:
        outcome = 'investigate'
        note = f'|Delta| = {abs(d) * 1e5:.0f} pcm > {ks:g} sigma_c = {ks * sc * 1e5:.0f} pcm'
    return {'outcome': outcome, 'k': k, 'sigma': sigma, 'k_ref': ref['value'], 'sigma_ref': ref['sigma'],
            'delta_pcm': d * 1e5, 'sigma_c_pcm': sc * 1e5, 'z': z, 'k_sigma': ks, 'note': note,
            'systematics': spec['known_systematics']}


def not_applicable(r, mods):
    """Goal modified source/derived quantities: report Delta, sigma_c, and |Delta|/sigma_c as not_applicable."""
    what = ', '.join(f"{m['label']} {m['old']:g}->{m['new']:g}" if isinstance(m['old'], (int, float))
                     and isinstance(m['new'], (int, float)) else f"{m['label']} {m['old']}->{m['new']}" for m in mods)
    return dict(r, outcome='not_applicable', note=f'Not applicable: goal modified source parameters ({what})',
                modifications=[{'param': m['param'], 'label': m['label'], 'old': m['old'], 'new': m['new'],
                                'unit': m['unit']} for m in mods])


def trial(spec, sigma_est, plist=None):
    """Evaluate synthetic trial cases (reference value, +2 sigma_c, -4 sigma_c, and single-parameter misreadings)."""
    ref = spec['reference']
    if ref is None:
        return {'sigma_est_pcm': sigma_est * 1e5, 'sigma_c_pcm': None, 'tolerance_pcm': None, 'rows': [],
                'note': 'No reference solution extracted from source'}
    sc = math.hypot(sigma_est, ref['sigma'])
    rows = []
    for label, k in (('Matches reference', ref['value']), ('+2 sigma_c offset', ref['value'] + 2 * sc),
                     ('-4 sigma_c offset', ref['value'] - 4 * sc)):
        r = evaluate(spec, k, sigma_est)
        rows.append({'label': label, 'k': k, 'outcome': r['outcome'], 'delta_pcm': r['delta_pcm']})
    tab = sensitivity_table(plist)
    for row in tab['rows']:
        if row['kind'] != 'misreading':
            continue
        k = ref['value'] + row['dk_pcm'] * 1e-5
        r = evaluate(spec, k, sigma_est)
        rows.append({'label': f"{row['id']} {row['what']}", 'k': k, 'outcome': r['outcome'],
                     'delta_pcm': r['delta_pcm']})
    detectable = 1e5 * spec['k_sigma'] * sc
    return {'sigma_est_pcm': sigma_est * 1e5, 'sigma_c_pcm': sc * 1e5, 'tolerance_pcm': detectable,
            'rows': rows,
            'note': f'Estimated tolerance is ~{detectable:.0f} pcm; single-parameter misreadings with smaller |Delta k| are below noise'}


def reading_key(pid, reading):
    """Key readings by content rather than index because swapping swaps the old reading back into the slot."""
    return f'{pid}={reading}'


def investigate(plist, observed_delta_pcm, tried=()):
    """Rank parameters with alternative readings by proximity of known Delta k to observed discrepancy.
    Tried readings (by reading_key) are omitted. Source-excluded readings are listed separately under
    excluded for control comparison only and are never treated as admissible candidates."""
    tab = sensitivity_table(plist)
    by_param = {}
    for row in tab['rows']:
        by_param.setdefault(row['param'], []).append(row)
    cands, excluded = [], []
    for p in plist['params']:
        for i, alt in enumerate(p['alternatives']):
            if reading_key(p['id'], alt['reading']) in tried:
                continue
            known = by_param.get(p['id'], [])
            best = min(known, key=lambda r: abs(r['dk_pcm'] - observed_delta_pcm)) if known else None
            row = {'param': p['id'], 'label': p['label'], 'alternative': i, 'reading': alt['reading'],
                   'note': alt.get('note', ''), 'known': best,
                   'distance_pcm': abs(best['dk_pcm'] - observed_delta_pcm) if best else None}
            (excluded if alt.get('status') == 'excluded' else cands).append(row)
    order = lambda c: (c['distance_pcm'] is None, c['distance_pcm'] or 0)
    cands.sort(key=order)
    excluded.sort(key=order)
    scale = [{'id': r['id'], 'param': r['param'], 'what': r['what'], 'dk_pcm': r['dk_pcm'],
              'sigma_pcm': r['sigma_pcm']} for r in tab['rows']]
    scale.sort(key=lambda r: -abs(r['dk_pcm']))
    return {'observed_delta_pcm': observed_delta_pcm, 'candidates': cands, 'excluded': excluded, 'scale': scale,
            'scale_source': tab['source'],
            'outcome': 'try_alternative' if cands else 'not_reproducible'}


def classes_for_param(pid, plist):
    return next((p['class'] for p in plist['params'] if p['id'] == pid), None)


def first_case_using(pid, plist):
    cls = classes_for_param(pid, plist)
    for case, c, _ in P.STEP_CLASSES:
        if c == cls:
            return case
    return None
