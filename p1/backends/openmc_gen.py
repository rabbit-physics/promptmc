"""OpenMC 0.15.3 input generator: physics model + binding choices -> model.xml with {field_path: [lines]} map."""
import os
import re
import warnings

from lxml import etree

REGION_ORDER = ('fuel', 'gap', 'clad', 'moderator')


def _geometry_ids(g):
    lattice = g.get('kind') == 'lattice'
    count = sum(len(rings) for rings in g['pins'].values()) if lattice else len(g['rings'])
    first = max(11, count + 1)
    planes = {name: first + i for i, name in enumerate(('x_min', 'x_max', 'y_min', 'y_max'))}
    lattice_id = max(20, len(g['pins']) + 2) if lattice else 20
    return planes, lattice_id, max(30, lattice_id + 1)


def _ordered_regions(model):
    return [r for r in REGION_ORDER if r in model['regions']] + \
        [r for r in model['regions'] if r not in REGION_ORDER]


def build_openmc_model(model, binding):
    import openmc
    openmc.reset_auto_ids()
    warnings.filterwarnings('ignore', category=openmc.IDWarning)
    regions = _ordered_regions(model)
    mats, mat_of = [], {}
    for i, rid in enumerate(regions, start=1):
        st = model['regions'][rid]
        mid = st['material']
        b = binding['materials'][mid]
        m = openmc.Material(material_id=i, name=model['materials'][mid].get('label') or mid)
        for n, f in b['nuclides'].items():
            m.add_nuclide(n, f, b['basis'])
        m.set_density('g/cm3', st['density_g_cm3'])
        m.temperature = st['temperature_K']
        if b.get('sab'):
            m.add_s_alpha_beta(b['sab'])
        mats.append(m)
        mat_of[rid] = m
    g = model['geometry']
    if g.get('kind') == 'lattice':
        geom, h = _lattice_geometry(openmc, g, mat_of)
        width = g['assembly_pitch_cm']
    else:
        geom, h = _pin_geometry(openmc, g, mat_of)
        width = g['pitch_cm']
    return _assemble(openmc, geom, mats, binding, h, width)


def _lattice_geometry(openmc, g, mat_of):
    planes, lattice_id, root_universe = _geometry_ids(g)
    if g['axial'] != 'infinite':
        raise ValueError('Lattice generator only supports infinite axial extent')
    p = g['rod_pitch_cm']
    n = len(g['layout'])
    universes, sid, cid = {}, 1, 1
    for u, (key, rings) in enumerate(g['pins'].items(), start=1):
        cells, inner = [], None
        for ring in rings:
            s = openmc.ZCylinder(surface_id=sid, r=ring['outer_radius_cm'], name=f"{key}/{ring['region']} outer")
            region = -s if inner is None else (+inner & -s)
            cells.append(openmc.Cell(cell_id=cid, name=f"{key}/{ring['region']}", fill=mat_of[ring['region']], region=region))
            inner = s
            sid += 1
            cid += 1
        out = g['outer_region']
        cells.append(openmc.Cell(cell_id=cid, name=f'{key}/{out}', fill=mat_of[out], region=+inner))
        cid += 1
        universes[key] = openmc.Universe(universe_id=u, cells=cells)
    outer = openmc.Universe(universe_id=len(g['pins']) + 1, cells=[
        openmc.Cell(cell_id=cid, name=f"outer/{g['outer_region']}", fill=mat_of[g['outer_region']])])
    cid += 1
    lat = openmc.RectLattice(lattice_id=lattice_id, name='assembly')
    lat.pitch = (p, p)
    lat.lower_left = (-n * p / 2.0, -n * p / 2.0)
    lat.universes = [[universes[ch] for ch in row] for row in g['layout']]
    lat.outer = outer
    h = g['assembly_pitch_cm'] / 2.0
    bc = g['radial_boundary']
    box = (+openmc.XPlane(surface_id=planes['x_min'], x0=-h, boundary_type=bc, name='x min')
           & -openmc.XPlane(surface_id=planes['x_max'], x0=h, boundary_type=bc, name='x max')
           & +openmc.YPlane(surface_id=planes['y_min'], y0=-h, boundary_type=bc, name='y min')
           & -openmc.YPlane(surface_id=planes['y_max'], y0=h, boundary_type=bc, name='y max'))
    root = openmc.Universe(universe_id=root_universe, cells=[openmc.Cell(cell_id=cid, name='assembly', fill=lat, region=box)])
    return openmc.Geometry(root), h


def _pin_geometry(openmc, g, mat_of):
    planes, _, _ = _geometry_ids(g)
    h = g['pitch_cm'] / 2.0
    bc = g['radial_boundary']
    cyl = {}
    for j, ring in enumerate(g['rings'], start=1):
        cyl[ring['region']] = openmc.ZCylinder(surface_id=j, r=ring['outer_radius_cm'],
                                               name=f"{ring['region']} outer")
    xmin = openmc.XPlane(surface_id=planes['x_min'], x0=-h, boundary_type=bc, name='x min')
    xmax = openmc.XPlane(surface_id=planes['x_max'], x0=h, boundary_type=bc, name='x max')
    ymin = openmc.YPlane(surface_id=planes['y_min'], y0=-h, boundary_type=bc, name='y min')
    ymax = openmc.YPlane(surface_id=planes['y_max'], y0=h, boundary_type=bc, name='y max')
    box = +xmin & -xmax & +ymin & -ymax
    cells, inner = [], None
    for k, ring in enumerate(g['rings'], start=1):
        rid = ring['region']
        region = -cyl[rid] if inner is None else (+inner & -cyl[rid])
        cells.append(openmc.Cell(cell_id=k, name=rid, fill=mat_of[rid], region=region))
        inner = cyl[rid]
    out = g['outer_region']
    cells.append(openmc.Cell(cell_id=len(cells) + 1, name=out, fill=mat_of[out], region=+inner & box))
    if g['axial'] != 'infinite':
        raise ValueError('Pincell generator only supports infinite axial extent')
    root = openmc.Universe(universe_id=1, cells=cells)
    return openmc.Geometry(root), h


def _assemble(openmc, geom, mats, binding, h, width):
    run = binding['run']
    st = openmc.Settings()
    st.particles, st.batches, st.inactive = run['particles'], run['batches'], run['inactive']
    st.seed = run['seed']
    st.source = openmc.IndependentSource(space=openmc.stats.Box((-h, -h, -1.0), (h, h, 1.0)),
                                         constraints={'fissionable': True})
    st.temperature = {'method': binding['temperature']['method'], 'default': binding['temperature']['default_K']}
    every = run.get('statepoint_every')
    if every:
        st.statepoint = {'batches': sorted(set(range(every, run['batches'] + 1, every)) | {run['batches']})}
    st.output = {'tallies': False}
    plot = openmc.Plot(plot_id=1, name='xy')
    plot.basis, plot.width, plot.pixels = 'xy', (width, width), (800, 800) if width > 5 else (400, 400)
    plot.color_by = 'material'
    plot.filename = 'geom_xy'
    return openmc.Model(geom, openmc.Materials(mats), st, plots=openmc.Plots([plot]))


def generate(model, binding, out_dir):
    """Write out_dir/model.xml and return (text, line_map)."""
    os.makedirs(out_dir, exist_ok=True)
    m = build_openmc_model(model, binding)
    path = os.path.join(out_dir, 'model.xml')
    m.export_to_model_xml(path)
    with open(path, encoding='utf-8') as f:
        text = f.read()
    return text, line_map(text, model)


def with_run(text, run, every=10):
    """Rewrite particles, batches, inactive, statepoint schedule (every N batches), and optional seed
    in an existing model.xml to match regenerating with those run parameters. Returns None if unrecognized."""
    sp = ' '.join(str(b) for b in sorted(set(range(every, run['batches'] + 1, every)) | {run['batches']}))
    for pat, val in ((r'(<particles>)\d+(</particles>)', run['particles']),
                     (r'(</particles>\s*<batches>)\d+(</batches>)', run['batches']),
                     (r'(<inactive>)\d+(</inactive>)', run['inactive']),
                     (r'(<state_point>\s*<batches>)[\d ]+(</batches>)', sp)):
        text, n = re.subn(pat, lambda m: f'{m.group(1)}{val}{m.group(2)}', text, count=1)
        if n != 1:
            return None
    if 'seed' in run:
        text, n = re.subn(r'(<seed>)\d+(</seed>)', lambda m: f"{m.group(1)}{run['seed']}{m.group(2)}", text, count=1)
        if n != 1:
            return None
    return text


def line_map(text, model):
    """Build {physics_field_path: [line_numbers]} from XML element sourcelines."""
    root = etree.fromstring(text.encode('utf-8'))
    regions = _ordered_regions(model)
    mat_el = {el.get('id'): el for el in root.iter('material')}
    surf_el = {el.get('id'): el for el in root.iter('surface')}
    cell_el = {el.get('id'): el for el in root.iter('cell')}
    out = {}

    def add(path, *els):
        out.setdefault(path, [])
        for e in els:
            if e is not None and e.sourceline not in out[path]:
                out[path].append(e.sourceline)

    g = model['geometry']
    plane_ids, _, _ = _geometry_ids(g)
    planes = [surf_el.get(str(i)) for i in plane_ids.values()]
    add('geometry.radial_boundary', *planes)
    if g.get('kind') == 'lattice':
        add('geometry.assembly_pitch_cm', *planes)
        lat = root.find('.//lattice')
        add('geometry.rod_pitch_cm', lat.find('pitch'))
        sid = 1
        for key, rings in g['pins'].items():
            for i in range(len(rings)):
                add(f'geometry.pin.{key}.{i}.outer_radius_cm', surf_el.get(str(sid)))
                sid += 1
        start = lat.find('universes').sourceline
        for i in range(len(g['layout'])):
            out[f'geometry.layout.{i}'] = [start + 1 + i]
        cell_of = {}
        for c in cell_el.values():
            cell_of.setdefault((c.get('name') or '').split('/')[-1], c)
    else:
        add('geometry.pitch_cm', *planes)
        for j, ring in enumerate(g['rings'], start=1):
            add(f"geometry.ring.{ring['region']}.outer_radius_cm", surf_el.get(str(j)))
        cell_of = None
    for i, rid in enumerate(regions, start=1):
        m = mat_el.get(str(i))
        c = cell_of.get(rid) if cell_of is not None else cell_el.get(str(i))
        add(f'regions.{rid}.material', c)
        add(f'regions.{rid}.temperature_K', m)
        add(f'regions.{rid}.density_g_cm3', m.find('density') if m is not None else None)
        mid = model['regions'][rid]['material']
        nuc = {n.get('name'): n for n in m.iter('nuclide')} if m is not None else {}
        mat = model['materials'][mid]
        r = mat['recipe']
        if r == 'uo2':
            add(f'materials.{mid}.u235_wt_pct', nuc.get('U235'), nuc.get('U238'))
            add(f'materials.{mid}.u234_wt_pct', nuc.get('U234'))
            add(f'materials.{mid}.u236_wt_pct', nuc.get('U236'))
            add(f'materials.{mid}.recipe', *nuc.values())
        elif r == 'borated_water':
            add(f'materials.{mid}.boron_ppm_wt', nuc.get('B10'), nuc.get('B11'))
            add(f'materials.{mid}.b10_at_pct', nuc.get('B10'), nuc.get('B11'))
            add(f'materials.{mid}.recipe', *nuc.values())
        else:
            for n, e in nuc.items():
                add(f'materials.{mid}.nuclides.{n}', e)
            add(f'materials.{mid}.recipe', *nuc.values())
        sab = m.find('sab') if m is not None else None
        if sab is not None:
            add(f'thermal_scattering.{mid}', sab)
    src = root.find('.//settings/source')
    if src is not None:
        add('source.kind', src)
    return out
