"""Deterministic validation rules: semantic rules operate on physics model fields without solver-specific
table names; binding rules check resolved solver binding approximations.

Verdicts: pass, warn (passes with warning), fail (blocking), na (not applicable), error (evaluation failure).
Manual overrides and unchecked states are tracked by the kernel at the evidence layer.
"""
import math

from ..domain import composition as C
from ..domain.model import label as L
from ..util import sha256

RULES = [
    {'id': 'RC-0002', 'name': 'MAT-05 UO2 Enrichment', 'severity': 'error', 'on_fail': 'block', 'enforcement': 'pre_run',
     'level': 'model', 'applies_when': 'Material is uranium oxide (O/U atomic ratio 1.9-2.1)',
     'check': 'U-235 weight percentage of uranium <= 5 passes, 5-20 warns, > 20 fails'},
    {'id': 'RC-0003', 'name': 'TSL-01 Thermal Scattering for Hydrogenous Moderator', 'severity': 'error', 'on_fail': 'block',
     'enforcement': 'pre_run', 'level': 'model', 'applies_when': 'Material contains hydrogen (atom fraction > 1%)',
     'check': 'Model declares bound thermal scattering (thermal_scattering) and binding implements it'},
    {'id': 'RC-0010', 'name': 'GEO-01 Concentric Rings and Cell Boundary', 'severity': 'error', 'on_fail': 'block',
     'enforcement': 'pre_run', 'level': 'model', 'applies_when': 'Pincell or lattice geometry templates',
     'check': 'Ring outer radii are > 0 and strictly increasing; outermost diameter is strictly less than pitch'},
    {'id': 'RC-0011', 'name': 'TEMP-01 Temperature Coverage', 'severity': 'error', 'on_fail': 'block',
     'enforcement': 'pre_run', 'level': 'binding', 'applies_when': 'All bindings',
     'check': 'Every region temperature has cross-section coverage: within interpolation bounds or listed as table approximation'},
    {'id': 'RC-0012', 'name': 'PHYS-01 Physical Density and Concentration Bounds', 'severity': 'error', 'on_fail': 'block',
     'enforcement': 'pre_run', 'level': 'model', 'applies_when': 'Fuel and moderator regions',
     'check': 'UO2 density <= 10.96 g/cm3 (theoretical density), water density <= 1.0 g/cm3, boron 0-5000 ppm'},
]
RULESET = sha256([{k: r[k] for k in ('id', 'check', 'applies_when', 'severity', 'on_fail')} for r in RULES])

UO2_TD = 10.96


def _res(rule, status, msg, fields=None):
    return {'rule': rule['id'], 'name': rule['name'], 'status': status, 'message': msg, 'fields': fields or []}


def _mat05(rule, model):
    out = []
    for mid, mat in model['materials'].items():
        p = C.properties(mat)
        if not p.get('uranium_oxide'):
            continue
        e = p['u235_wt_pct']
        f = [f'materials.{mid}.u235_wt_pct' if mat['recipe'] == 'uo2' else f'materials.{mid}.nuclides.U235']
        if e > 20:
            out.append(_res(rule, 'fail', f'{L(mid)} enrichment {e:.3f} wt% exceeds 20', f))
        elif e > 5:
            out.append(_res(rule, 'warn', f'{L(mid)} enrichment {e:.3f} wt% exceeds 5', f))
        else:
            out.append(_res(rule, 'pass', f'{L(mid)} enrichment {e:.4f} wt%', f))
    return out or [_res(rule, 'na', 'No uranium oxide materials')]


def _tsl(rule, model):
    out = []
    for mid, mat in model['materials'].items():
        if not C.properties(mat)['hydrogenous']:
            continue
        if model.get('thermal_scattering', {}).get(mid):
            out.append(_res(rule, 'pass', f"{L(mid)} declares {model['thermal_scattering'][mid]}",
                            [f'thermal_scattering.{mid}']))
        else:
            out.append(_res(rule, 'fail', f'{L(mid)} contains hydrogen without bound thermal scattering', [f'materials.{mid}.recipe']))
    return out or [_res(rule, 'na', 'No hydrogenous materials')]


def _geo_lattice(rule, model):
    g = model['geometry']
    p = g['rod_pitch_cm']
    out = []
    for key, rings in g['pins'].items():
        radii = [r['outer_radius_cm'] for r in rings]
        name = g.get('pin_labels', {}).get(key, key)
        if radii[0] <= 0 or any(not b > a for a, b in zip(radii, radii[1:])):
            out.append(_res(rule, 'fail', f"{name} radii are not strictly positive and increasing: {radii}",
                            [f'geometry.pin.{key}.{i}.outer_radius_cm' for i in range(len(radii))]))
        if 2 * radii[-1] >= p:
            out.append(_res(rule, 'fail', f"{name} outer diameter {2 * radii[-1]:g} cm >= rod pitch {p:g} cm",
                            ['geometry.rod_pitch_cm', f'geometry.pin.{key}.{len(radii) - 1}.outer_radius_cm']))
    n = len(g['layout'])
    if any(len(r) != n for r in g['layout']):
        out.append(_res(rule, 'fail', 'Lattice layout is not square', ['geometry.layout.0']))
    bad = sorted(set(''.join(g['layout'])) - set(g['pins']))
    if bad:
        out.append(_res(rule, 'fail', f"Lattice layout references undefined pin types: {bad}", ['geometry.layout.0']))
    if n * p > g['assembly_pitch_cm'] + 1e-9:
        out.append(_res(rule, 'fail', f"Pin array width {n * p:g} cm exceeds assembly pitch {g['assembly_pitch_cm']:g} cm",
                        ['geometry.rod_pitch_cm', 'geometry.assembly_pitch_cm']))
    if not out:
        count = {k: sum(r.count(k) for r in g['layout']) for k in g['pins']}
        out.append(_res(rule, 'pass', f"{n} x {n} lattice (" + ', '.join(f"{g.get('pin_labels', {}).get(k, k)} {v}" for k, v in count.items())
                        + f"), array width {n * p:g} cm, assembly pitch {g['assembly_pitch_cm']:g} cm", ['geometry.rod_pitch_cm']))
    return out


def _geo(rule, model):
    g = model['geometry']
    if g.get('kind') == 'lattice':
        return _geo_lattice(rule, model)
    radii = [r['outer_radius_cm'] for r in g['rings']]
    out = []
    first = g['rings'][0]
    if radii[0] <= 0:
        out.append(_res(rule, 'fail', f"{L(first['region'])} outer radius {radii[0]:g} cm <= 0",
                        [f"geometry.ring.{first['region']}.outer_radius_cm"]))
    for a, b, ra, rb in zip(g['rings'], g['rings'][1:], radii, radii[1:]):
        if not rb > ra:
            out.append(_res(rule, 'fail', f"{L(b['region'])} outer radius {rb:g} cm <= {L(a['region'])} {ra:g} cm",
                            [f"geometry.ring.{a['region']}.outer_radius_cm",
                             f"geometry.ring.{b['region']}.outer_radius_cm"]))
    last = g['rings'][-1]
    if 2 * radii[-1] >= g['pitch_cm']:
        out.append(_res(rule, 'fail', f"{L(last['region'])} outer diameter {2 * radii[-1]:g} cm >= pitch {g['pitch_cm']:g} cm",
                        ['geometry.pitch_cm', f"geometry.ring.{last['region']}.outer_radius_cm"]))
    if not out:
        frac = math.pi * radii[0] ** 2 / g['pitch_cm'] ** 2
        out.append(_res(rule, 'pass', f'Concentric rings strictly increasing, outer diameter {2 * radii[-1]:g} cm < pitch {g["pitch_cm"]:g} cm; '
                                      f'fuel area fraction {100 * frac:.1f}%', ['geometry.pitch_cm']))
    return out


def _phys(rule, model):
    out = []
    for rid, st in model['regions'].items():
        mat = model['materials'][st['material']]
        p = C.properties(mat)
        rho = st['density_g_cm3']
        f = [f'regions.{rid}.density_g_cm3']
        if p.get('uranium_oxide') and rho > UO2_TD:
            out.append(_res(rule, 'fail', f'{L(rid)} density {rho:g} g/cm3 exceeds UO2 theoretical density {UO2_TD}', f))
        if mat['recipe'] == 'borated_water':
            if rho > 1.0:
                out.append(_res(rule, 'fail', f'{L(rid)} water density {rho:g} g/cm3 exceeds 1.0', f))
            ppm = mat['boron_ppm_wt']
            if not 0 <= ppm <= 5000:
                out.append(_res(rule, 'fail', f'{L(rid)} boron concentration {ppm:g} ppm outside 0-5000',
                                [f"materials.{st['material']}.boron_ppm_wt"]))
        if rho <= 0:
            out.append(_res(rule, 'fail', f'{L(rid)} density must be positive', f))
    return out or [_res(rule, 'pass', 'Densities and concentrations within physical bounds')]


def _temp(rule, binding):
    out = []
    for it in binding['items']:
        if it['id'].startswith(('temperature.', 'tables.', 'sab.', 'temp.')):
            if it['status'] == 'missing_data':
                out.append(_res(rule, 'fail', f"{it['what']}: {it['detail']}", [it['id']]))
            elif it['status'] == 'approx':
                out.append(_res(rule, 'warn', f"{it['what']}: {it['detail']}", [it['id']]))
    return out or [_res(rule, 'pass', 'Temperature data covered for all regions')]


CHECKS = {'RC-0002': _mat05, 'RC-0003': _tsl, 'RC-0010': _geo, 'RC-0012': _phys}
BINDING_CHECKS = {'RC-0011': _temp}
ORDER = {'pass': 0, 'na': 0, 'warn': 1, 'error': 2, 'fail': 3}


def worst(results):
    st = 'pass'
    for r in results:
        if ORDER[r['status']] > ORDER[st]:
            st = r['status']
    if results and all(r['status'] == 'na' for r in results):
        st = 'na'
    return st


def evaluate_model(model):
    res = []
    for rule in RULES:
        fn = CHECKS.get(rule['id'])
        if not fn:
            continue
        try:
            res.extend(fn(rule, model))
        except Exception as e:  # Record evaluation failures explicitly instead of passing
            res.append(_res(rule, 'error', f'Evaluation error: {type(e).__name__}: {e}'))
    return {'ruleset': RULESET, 'status': worst(res), 'results': res}


def evaluate_binding(binding):
    res = []
    for rule in RULES:
        fn = BINDING_CHECKS.get(rule['id'])
        if not fn:
            continue
        try:
            res.extend(fn(rule, binding))
        except Exception as e:
            res.append(_res(rule, 'error', f'Evaluation error: {type(e).__name__}: {e}'))
    return {'ruleset': RULESET, 'status': worst(res), 'results': res}
