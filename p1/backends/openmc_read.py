"""OpenMC readback parser: reconstructs physics model and binding directly from written model.xml
without referencing generator objects.
"""
import re

from lxml import etree

from ..domain import composition as C

SAB_SEMANTIC = {'c_H_in_H2O': 'H in H2O'}
PINCELL_RINGS = {2: ('fuel', 'clad'), 3: ('fuel', 'gap', 'clad')}


class ReadError(Exception):
    pass


def _tokens(region):
    t = region.replace('(', ' ').replace(')', ' ')
    if '|' in t or '~' in t:
        raise ReadError(f'Region expression contains union or complement: {region}')
    return [int(x) for x in t.split()]


def _read_pin(surfs, cells, at, notes):
    cyls = {sid: s for sid, s in surfs.items() if s['type'] == 'z-cylinder'}
    for sid, s in cyls.items():
        if abs(s['coeffs'][0]) > 1e-12 or abs(s['coeffs'][1]) > 1e-12:
            raise ReadError(f'Cylinder surface {sid} is not centered at the origin')
    xs = {sid: s for sid, s in surfs.items() if s['type'] == 'x-plane'}
    ys = {sid: s for sid, s in surfs.items() if s['type'] == 'y-plane'}
    zs = {sid: s for sid, s in surfs.items() if s['type'] == 'z-plane'}
    if len(xs) != 2 or len(ys) != 2:
        raise ReadError('Cell boundary does not consist of two pairs of x and y planes')
    xv = sorted(s['coeffs'][0] for s in xs.values())
    yv = sorted(s['coeffs'][0] for s in ys.values())
    if abs(xv[0] + xv[1]) > 1e-9 or abs(yv[0] + yv[1]) > 1e-9 or abs((xv[1] - xv[0]) - (yv[1] - yv[0])) > 1e-9:
        raise ReadError('Cell boundary is not an origin-centered square')
    pitch = xv[1] - xv[0]
    bcs = {s['boundary'] for s in list(xs.values()) + list(ys.values())}
    if len(bcs) != 1:
        raise ReadError(f'Boundary conditions differ across the four outer planes: {sorted(bcs)}')
    bc = bcs.pop()
    for s in list(xs.values()) + list(ys.values()):
        at('geometry.pitch_cm', s['el'])
        at('geometry.radial_boundary', s['el'])
    if zs:
        axial = 'infinite' if all(s['boundary'] == 'reflective' for s in zs.values()) else 'finite'
        notes.append('Axial extent bounded by two z-planes' + (', reflective at both ends' if axial == 'infinite' else ''))
    else:
        axial = 'infinite'
    radii = sorted(cyls, key=lambda sid: cyls[sid]['coeffs'][2])
    names = PINCELL_RINGS.get(len(radii))
    if not names:
        raise ReadError(f'Ring count {len(radii)} is not 2 or 3 as supported by the pincell template')
    order = {sid: i for i, sid in enumerate(radii)}
    plane_ids = set(xs) | set(ys) | set(zs)
    ring_cell, outer_cell = {}, None
    for c in cells:
        cyl_t = [t for t in c['tokens'] if abs(t) in cyls]
        if any(abs(t) in plane_ids for t in c['tokens']) or (cyl_t and all(t > 0 for t in cyl_t)):
            if cyl_t != [radii[-1]] or outer_cell:
                raise ReadError(f"Cell {c['id']} is not the exterior region outside the rings")
            outer_cell = c
            continue
        neg = [t for t in cyl_t if t < 0]
        pos = [t for t in cyl_t if t > 0]
        if len(neg) != 1 or len(pos) > 1:
            raise ReadError(f"Cell {c['id']} does not represent a single concentric ring")
        k = order[-neg[0]]
        if (k == 0 and pos) or (k > 0 and (not pos or order[pos[0]] != k - 1)):
            raise ReadError(f"Cell {c['id']} inner and outer surfaces are not adjacent")
        ring_cell[k] = c
    if len(ring_cell) != len(radii) or outer_cell is None:
        raise ReadError('A concentric ring is missing its corresponding cell')
    rings = []
    region_cells = {}
    for k, sid in enumerate(radii):
        rid = names[k]
        rings.append({'region': rid, 'outer_radius_cm': cyls[sid]['coeffs'][2]})
        at(f'geometry.ring.{rid}.outer_radius_cm', cyls[sid]['el'])
        region_cells[rid] = ring_cell[k]
    region_cells['moderator'] = outer_cell
    return ({'pitch_cm': pitch, 'rings': rings, 'outer_region': 'moderator', 'radial_boundary': bc, 'axial': axial},
            region_cells)


def _read_lattice(root, surfs, cells, at, loc):
    """Square lattice: pin type is keyed by prefix before '/', with radii, materials, and grid read from XML."""
    lat = root.find('.//lattice')
    dim = [int(x) for x in lat.find('dimension').text.split()]
    if len(dim) != 2 or dim[0] != dim[1]:
        raise ReadError(f'Lattice is not square: {dim}')
    n = dim[0]
    pitch = [float(x) for x in lat.find('pitch').text.split()]
    if len(pitch) != 2 or abs(pitch[0] - pitch[1]) > 1e-12:
        raise ReadError(f'Lattice x and y pitches differ: {pitch}')
    ll = [float(x) for x in lat.find('lower_left').text.split()]
    if abs(ll[0] + n * pitch[0] / 2) > 1e-9 or abs(ll[1] + n * pitch[0] / 2) > 1e-9:
        raise ReadError(f'Lattice is not centered at the origin: {ll}')
    at('geometry.rod_pitch_cm', lat.find('pitch'))
    ids = lat.find('universes').text.split()
    if len(ids) != n * n:
        raise ReadError(f'Lattice has {len(ids)} entries, expected {n * n}')
    by_u = {}
    for c in cells:
        by_u.setdefault(c['universe'], []).append(c)
    fill_cells = [c for c in cells if c['fill'] is not None]
    if len(fill_cells) != 1 or fill_cells[0]['fill'] != lat.get('id'):
        raise ReadError('Root cell does not fill the lattice')
    top = fill_cells[0]
    planes = {abs(t): surfs[abs(t)] for t in top['tokens']}
    xs = sorted(s['coeffs'][0] for s in planes.values() if s['type'] == 'x-plane')
    ys = sorted(s['coeffs'][0] for s in planes.values() if s['type'] == 'y-plane')
    if len(xs) != 2 or len(ys) != 2 or abs(xs[0] + xs[1]) > 1e-9 or abs(ys[0] + ys[1]) > 1e-9 or abs((xs[1] - xs[0]) - (ys[1] - ys[0])) > 1e-9:
        raise ReadError('Assembly boundary is not an origin-centered square')
    if any((t > 0) != (surfs[abs(t)]['coeffs'][0] < 0) for t in top['tokens']):
        raise ReadError('Root cell is not bounded inside the four outer planes')
    bcs = {s['boundary'] for s in planes.values()}
    if len(bcs) != 1:
        raise ReadError(f'Boundary conditions differ across the four outer planes: {sorted(bcs)}')
    for s in planes.values():
        at('geometry.assembly_pitch_cm', s['el'])
        at('geometry.radial_boundary', s['el'])
    pins, key_of, region_cells, material_of = {}, {}, {}, {}

    def note_region(rid, c):
        if rid in material_of and material_of[rid] != c['material']:
            raise ReadError(f"Region {rid} uses two different materials: {material_of[rid]} and {c['material']}")
        material_of[rid] = c['material']
        region_cells.setdefault(rid, c)

    for u in sorted(set(ids), key=int):
        members = by_u.get(u)
        if not members:
            raise ReadError(f'Universe {u} in lattice has no cells')
        key = (members[0]['name'] or '').split('/')[0]
        if not key or any((c['name'] or '').split('/')[0] != key for c in members):
            raise ReadError(f'Cell name prefixes are inconsistent in universe {u}')
        key_of[u] = key
        cyl_ids = {abs(t) for c in members for t in c['tokens']}
        if any(surfs[i]['type'] != 'z-cylinder' or abs(surfs[i]['coeffs'][0]) > 1e-12 or abs(surfs[i]['coeffs'][1]) > 1e-12 for i in cyl_ids):
            raise ReadError(f'Not all surfaces in pin {key} are origin-centered z-cylinders')
        cyl_ids = sorted(cyl_ids, key=lambda i: surfs[i]['coeffs'][2])
        order = {sid: i for i, sid in enumerate(cyl_ids)}
        rings, out_cell = [None] * len(cyl_ids), None
        for c in members:
            neg = [t for t in c['tokens'] if t < 0]
            pos = [t for t in c['tokens'] if t > 0]
            if not neg:
                if pos != [cyl_ids[-1]] or out_cell:
                    raise ReadError(f"Cell {c['id']} is not the exterior region outside the rings")
                out_cell = c
                continue
            k = order[-neg[0]]
            if len(neg) != 1 or (k == 0 and pos) or (k > 0 and pos != [cyl_ids[k - 1]]):
                raise ReadError(f"Cell {c['id']} does not represent a single concentric ring")
            rid = c['name'].split('/')[1]
            rings[k] = {'region': rid, 'outer_radius_cm': surfs[-neg[0]]['coeffs'][2]}
            at(f'geometry.pin.{key}.{k}.outer_radius_cm', surfs[-neg[0]]['el'])
            note_region(rid, c)
        if out_cell is None or any(r is None for r in rings):
            raise ReadError(f'Pin {key} has a ring without a matching cell')
        note_region(out_cell['name'].split('/')[1], out_cell)
        pins[key] = rings
    outer = lat.find('outer')
    outer_cells = by_u.get(outer.text.strip(), []) if outer is not None else []
    if len(outer_cells) != 1 or outer_cells[0]['tokens']:
        raise ReadError('Outer lattice universe must contain a single unbounded material cell')
    out_rid = outer_cells[0]['name'].split('/')[1]
    note_region(out_rid, outer_cells[0])
    layout = [''.join(key_of[u] for u in ids[r * n:(r + 1) * n]) for r in range(n)]
    start = lat.find('universes').sourceline
    for r in range(n):
        loc.setdefault(f'geometry.layout.{r}', []).append(start + 1 + r)
    return ({'kind': 'lattice', 'rod_pitch_cm': pitch[0], 'assembly_pitch_cm': xs[1] - xs[0], 'pins': pins, 'layout': layout,
             'outer_region': out_rid, 'radial_boundary': bcs.pop(), 'axial': 'infinite'}, region_cells)


def _box_covers_fuel(sp, geometry):
    """Initial fission source box must be origin-centered, cover all fuel, and remain inside the outer boundary."""
    p = sp.find('parameters')
    vals = [float(x) for x in (p.text if p is not None else sp.get('parameters', '')).split()]
    if len(vals) != 6:
        return False
    x0, y0, z0, x1, y1, z1 = vals
    if abs(x0 + x1) > 1e-9 or abs(y0 + y1) > 1e-9 or abs(x0 - y0) > 1e-9 or not z0 < 0 < z1:
        return False
    if geometry.get('kind') == 'lattice':
        low, high = len(geometry['layout']) * geometry['rod_pitch_cm'] / 2, geometry['assembly_pitch_cm'] / 2
    else:
        low, high = geometry['rings'][0]['outer_radius_cm'], geometry['pitch_cm'] / 2
    return low - 1e-9 <= x1 <= high + 1e-9


def read(text):
    root = etree.fromstring(text.encode('utf-8'))
    loc, notes, unread = {}, [], []

    def at(path, el):
        if el is not None:
            loc.setdefault(path, []).append(el.sourceline)

    mats = {}
    for el in root.iter('material'):
        d = el.find('density')
        if d is None:
            raise ReadError(f"Material {el.get('id')} has no density element")
        units = d.get('units')
        val = float(d.get('value')) if d.get('value') is not None else None
        if units == 'kg/m3':
            val, units = val / 1000.0, 'g/cm3'
        if units != 'g/cm3':
            raise ReadError(f"Material {el.get('id')} density unit {units} is outside readback scope")
        nucs, basis = {}, None
        for n in el.iter('nuclide'):
            b = 'ao' if n.get('ao') is not None else 'wo'
            if basis and b != basis:
                raise ReadError(f"Material {el.get('id')} mixes atom fractions and weight fractions")
            basis = b
            nucs[n.get('name')] = float(n.get(b))
        if el.find('element') is not None:
            raise ReadError(f"Material {el.get('id')} contains an <element> tag")
        sab = [s.get('name') for s in el.iter('sab')]
        t = el.get('temperature')
        mats[el.get('id')] = {'el': el, 'name': el.get('name'), 'density': val, 'dens_el': d,
                              'basis': basis, 'nuclides': nucs, 'sab': sab,
                              'temperature': float(t) if t else None}
    surfs = {}
    for el in root.iter('surface'):
        coeffs = [float(x) for x in el.get('coeffs').split()]
        surfs[int(el.get('id'))] = {'el': el, 'type': el.get('type'), 'coeffs': coeffs,
                                    'boundary': el.get('boundary', 'transmission')}
    cells = []
    for el in root.iter('cell'):
        if el.get('temperature') is not None:
            raise ReadError(f"Cell {el.get('id')} sets a cell-level temperature distinct from region temperature")
        if el.get('translation') is not None or el.get('rotation') is not None:
            raise ReadError(f"Cell {el.get('id')} uses coordinate transforms unsupported by readback")
        cells.append({'el': el, 'id': int(el.get('id')), 'name': el.get('name'), 'universe': el.get('universe', '0'),
                      'fill': el.get('fill'), 'material': el.get('material'), 'tokens': _tokens(el.get('region', ''))})
    settings = root.find('settings')

    def num(tag, cast=float):
        e = settings.find(tag) if settings is not None else None
        return cast(e.text) if e is not None and e.text else None

    t_default = None
    t_method = None
    if settings is not None:
        td = settings.find('temperature_default')
        tm = settings.find('temperature_method')
        t_default = float(td.text) if td is not None else 293.6
        t_method = tm.text if tm is not None else 'nearest'
    if root.find('.//lattice') is not None:
        geometry, region_cells = _read_lattice(root, surfs, cells, at, loc)
    else:
        geometry, region_cells = _read_pin(surfs, cells, at, notes)
    regions, materials, tsl = {}, {}, {}
    bind_mats = {}
    for rid, c in region_cells.items():
        m = mats.get(c['material'])
        if m is None:
            raise ReadError(f"Cell {c['id']} references missing material {c['material']}")
        temp = m['temperature'] if m['temperature'] is not None else t_default
        regions[rid] = {'material': rid, 'density_g_cm3': m['density'], 'temperature_K': temp}
        at(f'regions.{rid}.material', c['el'])
        at(f'regions.{rid}.density_g_cm3', m['dens_el'])
        at(f'regions.{rid}.temperature_K', m['el'])
        materials[rid] = C.recognize(m['basis'], m['nuclides'], label=m['name'] or '')
        for n_el in m['el'].iter('nuclide'):
            at(f'materials.{rid}.nuclides.{n_el.get("name")}', n_el)
            at(f'materials.{rid}.recipe', n_el)
        for s in m['sab']:
            sem = SAB_SEMANTIC.get(s)
            if sem:
                tsl[rid] = sem
            else:
                notes.append(f'Thermal scattering table {s} on material {rid} has no semantic mapping')
        for s_el in m['el'].iter('sab'):
            at(f'thermal_scattering.{rid}', s_el)
        bind_mats[rid] = {'sab': m['sab'][0] if m['sab'] else None,
                          'basis': m['basis'], 'nuclides': m['nuclides']}
    src, src_dist = {}, None
    if settings is not None:
        s_el = settings.find('source')
        if s_el is not None:
            sp = s_el.find('space')
            cons = s_el.find('constraints')
            fiss = cons is not None and cons.find('fissionable') is not None and \
                cons.find('fissionable').text.strip().lower() == 'true'
            src = {'kind': 'fission_guess'}
            src_dist = 'box_fissionable' if sp is not None and sp.get('type') == 'box' and fiss and _box_covers_fuel(sp, geometry) else 'other'
            at('source.kind', s_el)
        for tag in settings:
            if tag.tag not in ('run_mode', 'particles', 'batches', 'inactive', 'seed', 'source',
                               'temperature_default', 'temperature_method', 'output', 'state_point'):
                unread.append(f'settings/{tag.tag}')
    model = {'schema': 'p1.model/1', 'template': 'lattice' if geometry.get('kind') == 'lattice' else 'pincell',
             'geometry': geometry,
             'regions': regions, 'materials': materials, 'thermal_scattering': tsl, 'source': src}
    sp_el = settings.find('state_point') if settings is not None else None
    sp_batches = [int(x) for x in sp_el.find('batches').text.split()] if sp_el is not None else []
    binding = {'solver': 'openmc', 'materials': bind_mats,
               'temperature': {'method': t_method, 'default_K': t_default},
               'run': {'mode': num('run_mode', str),
                       'particles': num('particles', int), 'batches': num('batches', int),
                       'inactive': num('inactive', int), 'seed': num('seed', int),
                       'statepoint_batches': sp_batches, 'source': src_dist}}
    for tag in ('plots', 'tallies'):
        if root.find(tag) is not None and tag == 'tallies':
            unread.append('tallies')
    return {'model': model, 'binding': binding, 'locations': loc, 'notes': notes, 'uninterpreted': unread}


_WS = re.compile(r'\s+')
