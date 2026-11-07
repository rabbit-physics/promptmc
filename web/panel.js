'use strict';
/* Workspace panel: params, source, model, input, evidence, report */

function panelKey() {
  const sn = S.snap;
  if (!sn || !S.panel) return 'none';
  const c = sn.cases.find(x => x.case === S.mcase) || {};
  switch (S.panel) {
    case 'params': return ['p', sn.goal.spec.paramlist, JSON.stringify(S.pendingReading), sn.goal.archived, JSON.stringify(sn.rules || null),
      sn.cases.map(x => (x.rules || {}).id).join(), JSON.stringify(sn.extraction ? [sn.extraction.state, sn.extraction.blocking, sn.extraction.review] : null),
      JSON.stringify([S.rv.scope, S.rv.only, S.rv.sel, S.rv.adding, S.rv.open, S.rv.want, S.src, S.srcZoom, S.checkLlmKey]), splitOn()].join('|');
    case 'model': return ['m', S.mcase, JSON.stringify(c.stages || {}), S.mfield, !!S.preview, S.plotOn, sn.goal.archived].join('|');
    case 'input': return ['i', S.mcase, S.msolver, JSON.stringify(c.stages || {}), S.mfield,
      (c['readback.openmc'] || {}).status, (c['readback.ref_mc'] || {}).status].join('|');
    case 'source': return ['s', JSON.stringify(S.src), S.srcZoom].join('|');
    case 'evidence': return ['e', S.evidence.length, S.evSel].join('|');
    case 'report': return ['r', (sn.report || {}).hash, S.reportHash, sn.goal.archived].join('|');
  }
  return 'none';
}

async function renderPanel(force = true) {
  const el = $('#pbody'), seg = $('#pseg');
  if (!S.panel || !S.snap) return;
  const segH = panelsNow().map(([k, l]) => `<button role="tab" type="button" data-ptab="${k}" aria-selected="${S.panel === k}">${l}</button>`).join('');
  if (seg.dataset.h !== segH) { seg.innerHTML = segH; seg.dataset.h = segH; }
  const key = panelKey();
  if (!force && key === S.panelKey) return;
  S.panelKey = key;
  if (S.panel === 'params' && splitOn()) return renderSplit(key);
  if (el.classList.contains('split')) { el.classList.remove('split'); el.innerHTML = ''; }
  const fn = {params: viewParams, source: viewSource, model: viewModel, input: viewInput, evidence: viewEvidence, report: viewReport}[S.panel];
  try {
    const html = await fn();
    if (html !== undefined && S.panelKey === key) { S.rvHtml = null; patchHtml(el, html, 'rvHtml'); }
  } catch (e) { el.innerHTML = `<p class="mut">${h(e.message)}</p>`; }
  if (S.panel === 'params') afterReview();
  if (S.panel === 'model' || S.panel === 'input') afterModel();
}

/* ---------- Source ---------- */
async function viewSource() {
  const docs = S.snap.docs || [];
  if (!docs.length) return '';
  if (!S.src || !docs.some(x => x.hash === S.src.hash)) S.src = {hash: docs[0].hash, page: 1, box: null, quote: ''};
  const s = S.src, m = docs.find(x => x.hash === s.hash);
  const tools = `<div class="ptools">
    ${docs.length > 1 ? `<select id="srcDoc" aria-label="Document">${docs.map(x => `<option value="${x.hash}"${x.hash === s.hash ? ' selected' : ''}>${h(x.name)}</option>`).join('')}</select>` : `<span class="srcn" title="${h(m.name)}">${h(m.name)}</span>`}
    ${m.pages > 1 ? `<span class="srcp"><button class="ib sm" type="button" data-act="src-prev" aria-label="Previous page" title="Previous page" ${s.page <= 1 ? 'disabled' : ''}>${ic('back')}</button>
      <input class="num" id="srcPage" inputmode="numeric" value="${s.page}" aria-label="Page number"><span class="num mut">/ ${m.pages}</span>
      <button class="ib sm" type="button" data-act="src-next" aria-label="Next page" title="Next page" ${s.page >= m.pages ? 'disabled' : ''}>${ic('chev')}</button></span>` : ''}
    ${m.kind === 'pdf' || m.kind === 'image' ? `<span class="srcz"><button class="ib sm" type="button" data-src-zoom="-1" aria-label="Zoom out" title="Zoom out" ${S.srcZoom <= 1 ? 'disabled' : ''}>${ic('minus')}</button><button class="ib sm" type="button" data-src-zoom="1" aria-label="Zoom in" title="Zoom in" ${S.srcZoom >= 3 ? 'disabled' : ''}>${ic('plus')}</button></span>` : ''}</div>`;
  if (m.kind === 'pdf' || m.kind === 'image') {
    const b = s.box;
    const mark = b ? `<i class="srcbox" style="left:${100 * (b[0] - 2) / b[4]}%;top:${100 * (b[1] - 2) / b[5]}%;width:${100 * (b[2] - b[0] + 4) / b[4]}%;height:${100 * (b[3] - b[1] + 4) / b[5]}%"></i>` : '';
    return `${tools}<div class="srcw"><div class="srcv" style="width:${100 * S.srcZoom}%"><img src="api/docs/${s.hash}/page/${s.page}.png?dpi=${S.srcZoom > 1 ? 220 : 150}" alt="${h(m.name)} page ${s.page}">${mark}</div></div>`;
  }
  const t = await api(`api/docs/${s.hash}/text/${s.page}`);
  let body = h(t.text);
  const words = h(s.quote || '').split(/\s+/).filter(Boolean);
  if (words.length) body = body.replace(new RegExp(words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+')), x => `<mark>${x}</mark>`);
  return `${tools}<pre class="srctext">${body}</pre>`;
}

async function viewParams() {
  const pl = (await node(S.snap.goal.spec.paramlist)).content;
  const mods = {}; (S.snap.modifications || []).forEach(m => { mods[m.param] = m; });
  const ex = S.snap.extraction, edit = ex && ex.state === 'review' && !ro() && !ex.list_only;
  if (ex) return reviewHtml(pl, ex, `${pl.params.filter(p => p.kind === 'figure' && p.value).map(p => `<section class="psec"><h3>${h(p.label)}</h3>${figureRows(p)}</section>`).join('')}
    ${edit ? '' : pl.params.filter(p => p.id === 'layout' && p.value).map(p => `<section class="psec"><h3>Pin layout</h3>${layoutSvg(p, false)}${(p.evidence || []).map(x => `<p class="mut">${h(x)}</p>`).join('')}</section>`).join('')}
    ${ex.state === 'confirmed' ? await rulesHtml() : ''}
    ${pl.reference ? refHtml(pl.reference) : ''}`);
  return `<ul class="plist">${pl.params.map(p => {
    const m = mods[p.id];
    const k = `pp:${p.id}`;
    return `<li><details data-k="${k}"${S.open[k] ? ' open' : ''}><summary>${natMark(m ? 'edited' : p.nature)}<span class="pl-l">${h(p.label)}</span>
        <span class="pl-v num">${typeof p.value === 'number' ? h(+p.value.toPrecision(7)) : h(showVal(p.value))} <span class="u">${h(unit(p.unit))}</span></span>
        ${p.alternatives.length ? `<span class="count num" title="Alternative readings">${p.alternatives.length}</span>` : ''}</summary>
      <div class="pl-d">
        <div class="pl-id mono">${h(p.id)}</div>
        ${m ? `<div><span class="mut">Modified via</span> ${h(m.origin)}</div><div><span class="mut">Original</span> <span class="num">${h(m.old)}</span> ${h(unit(p.unit))}, ${h(m.source)}</div><blockquote>${h(m.quote)}</blockquote>`
          : `<div>${h(p.source)}</div>${p.quote ? `<blockquote>${h(p.quote)}</blockquote>` : ''}`}
        <div><span class="mut">Reading</span> ${h(p.reading)}</div>
        ${p.alternatives.map((a, i) => `<div class="alt"><span class="mut">Alternative</span> ${h(a.reading)}${a.value !== p.value ? ` <span class="num">${h(a.value)}</span> ${h(unit(a.unit || p.unit))}` : ''}${a.note ? ` <span class="mut">${h(a.note)}</span>` : ''}
          ${ro() ? '' : S.pendingReading[`${p.id}:${i}`]
            ? `<span class="acts in"><button class="btn sm pri" data-reading="${h(p.id)}:${i}" data-sure="1" type="button">Confirm switch</button><button class="btn sm" data-reading-cancel="${h(p.id)}:${i}" type="button">Cancel</button></span>`
            : `<span class="acts in"><button class="btn sm" data-reading="${h(p.id)}:${i}" type="button">Use reading</button></span>`}</div>`).join('')}
      </div></details></li>`;
  }).join('')}</ul>
  ${await rulesHtml()}
  ${refHtml(pl.reference)}`;
}
function refHtml(r) {
  return `<section class="psec"><h3>Reference</h3><dl class="kv"><dt>k</dt><dd class="num">${r.value} ± ${r.sigma}</dd>
    <dt>Method</dt><dd>${h(r.method)}</dd><dt>Source</dt><dd>${h(r.source)}</dd>
    <dt>Quote</dt><dd class="mono sm">${h(r.quote)}</dd></dl></section>`;
}

/* ---------- Model ---------- */
function fieldLabel(path, pl) {
  const parts = path.split('.');
  const reg = r => LABEL[r] || r;
  if (path === 'geometry.pitch_cm' || path === 'geometry.rod_pitch_cm') return 'Pitch (cm)';
  if (path === 'geometry.assembly_pitch_cm') return 'Assembly pitch (cm)';
  if (parts[1] === 'layout') return `Layout row ${+parts[2] + 1}`;
  if (parts[1] === 'pin') {
    const pin = {F: 'Fuel pin', G: 'Guide tube', I: 'Instrument tube'}[parts[2]] || parts[2];
    return parts[3] === 'ring_order' ? null : `${pin} layer ${+parts[3] + 1} outer radius (cm)`;
  }
  if (path === 'geometry.kind') return null;
  if (parts[1] === 'ring') return `${reg(parts[2])} outer radius (cm)`;
  if (path === 'geometry.radial_boundary') return 'Radial boundary';
  if (path === 'geometry.axial') return 'Axial';
  if (path === 'geometry.ring_order') return pl === undefined ? 'Ring order' : null;
  if (path === 'geometry.outer_region') return pl === undefined ? 'Outer region' : null;
  if (parts[0] === 'regions') return `${reg(parts[1])} ${{density_g_cm3: 'density (g/cm³)', temperature_K: 'temperature (K)', material: 'material'}[parts[2]] || parts[2]}`;
  if (parts[0] === 'materials') {
    const f = {u235_wt_pct: 'U-235 (wt%)', u234_wt_pct: 'U-234 (wt%)', u236_wt_pct: 'U-236 (wt%)', boron_ppm_wt: 'boron (ppm)', b10_at_pct: 'B-10 (at%)', recipe: 'recipe', basis: 'basis'}[parts[2]];
    if (parts[2] === 'nuclides') return `${reg(parts[1])} ${parts[3]} fraction`;
    return `${reg(parts[1])} ${f || parts[2]}`;
  }
  if (parts[0] === 'thermal_scattering') return `${reg(parts[1])} thermal scattering`;
  if (parts[0] === 'source') return 'Initial source';
  return path;
}
const VAL = {reflective: 'Reflective', vacuum: 'Vacuum', infinite: 'Infinite', finite: 'Finite', uo2: 'UO2', borated_water: 'Borated water',
  nuclides: 'Nuclide fractions', fission_guess: 'Fission source guess', 'H in H2O': 'H in H2O', wo: 'Weight fraction', ao: 'Atom fraction'};
function showVal(v) { return typeof v === 'number' ? +v.toPrecision(6) : (VAL[v] || LABEL[v] || v); }
function regionOf(path) {
  const p = path.split('.');
  if (p[0] === 'regions' || p[0] === 'materials' || p[0] === 'thermal_scattering') return p[1];
  if (p[0] === 'geometry' && p[1] === 'ring') return p[2];
  if (path === 'geometry.pitch_cm' || path === 'geometry.radial_boundary' || path === 'geometry.rod_pitch_cm' || path === 'geometry.assembly_pitch_cm') return 'moderator';
  return null;
}
function casePick() {
  return `<div class="seg seg-s" role="group" aria-label="Case">${S.snap.cases.map(x => `<button type="button" data-mcase="${h(x.case)}" aria-pressed="${x.case === S.mcase}" title="${h(x.label)}">${h(x.case)}</button>`).join('')}</div>`;
}
async function loadModelCtx() {
  const sn = S.snap;
  const c = sn.cases.find(x => x.case === S.mcase) || sn.cases[sn.cases.length - 1];
  S.mcase = c.case;
  const st = k => c.stages[k] && c.stages[k].node;
  const [mn, on, cn, pn] = await Promise.all([node(st('model')), node(st('gen.openmc')), node(st('gen.ref_mc')), node(sn.goal.spec.paramlist)]);
  S.mctx = {c, m: mn && mn.content, on, cn, pl: pn && pn.content, fields: mn ? mn.fields : {}};
  return S.mctx;
}
async function viewModel() {
  const {c, m, pl, fields} = await loadModelCtx();
  const pm = {}; (pl ? pl.params : []).forEach(p => { pm[p.id] = p; });
  const prov = m ? m.provenance : {};
  const rows = Object.entries(fields).map(([k, v]) => [k, v, fieldLabel(k, pl)])
    .filter(r => r[2] && !/\.(material|basis)$/.test(r[0]) && !(r[0].includes('.nuclides.') && r[0].startsWith('materials.clad')));
  return `<div class="ptools">${casePick()}
      <div class="seg seg-s" role="group" aria-label="Cross section view"><button class="" data-plot="0" type="button" aria-pressed="${!S.plotOn}">Schematic</button>
        <button data-plot="1" type="button" aria-pressed="${S.plotOn}">OpenMC Plot</button></div></div>
    ${S.preview ? `<div class="card">${previewBody(S.preview, 'mpv')}<div class="acts"><button class="btn pri" data-act="apply-form" type="button" ${S.preview.cases.length ? '' : 'disabled'}>Apply</button>
      <button class="btn" data-act="unpreview" type="button">Discard</button></div></div>` : ''}
    <div class="mv">
      <div class="xsbox" id="xsBox">${S.plotOn ? `<img class="xs" id="omcPlot" alt="OpenMC xy plot for ${h(c.case)}">` : xsSvg(m, S.mfield)}</div>
      <div id="fieldInfo">${fieldInfo()}</div>
    </div>
    <div class="fields" role="listbox" aria-label="Model fields">${rows.map(([k, v, lb]) => {
      const pid = prov[k];
      const src = !pid ? '' : pid.startsWith('template:') ? 'tpl' : 'pl';
      return `<button type="button" role="option" aria-selected="${k === S.mfield}" class="${k === S.mfield ? 'on' : ''}" data-field="${h(k)}">
        <span class="fl">${h(lb)}</span>${src ? `<span class="src-${src}" title="${src === 'tpl' ? 'Template default' : 'Param: ' + h((pm[pid] || {}).label || pid)}"></span>` : ''}<span class="fv num">${h(showVal(v))}</span></button>`;
    }).join('')}</div>`;
}
function fieldInfo() {
  const sel = S.mfield, mc = S.mctx;
  if (!sel || !mc || !mc.m) return '';
  const pm = {}; (mc.pl ? mc.pl.params : []).forEach(p => { pm[p.id] = p; });
  const pid = mc.m.provenance[sel];
  const editable = !ro() && pid && !pid.startsWith('template:') && pm[pid] && typeof pm[pid].value === 'number';
  const ln = n => ((n && n.line_map[sel]) || []);
  const lines = (solver, n) => ln(n).length ? `<button class="lnk num" type="button" data-panel="input" data-solver="${solver}">${h(ln(n).join(', '))}</button>` : '<span class="mut">—</span>';
  return `<div class="finfo"><div class="fi-h"><b>${h(fieldLabel(sel, mc.pl))}</b><span class="num">${h(showVal(mc.fields[sel]))}</span></div>
    <dl class="kv"><dt>Source</dt><dd>${pid ? (pid.startsWith('template:') ? 'Template default' : `Parameter "${h((pm[pid] || {}).label || pid)}"`) : '—'}</dd>
      <dt>OpenMC lines</dt><dd>${lines('openmc', mc.on)}</dd><dt>Ref MC lines</dt><dd>${lines('ref_mc', mc.cn)}</dd></dl>
    ${editable ? `<div class="edit"><label><span class="sr">New value</span><input class="num" type="number" step="any" id="editVal" value="${h(pm[pid].value)}"></label><span>${h(unit(pm[pid].unit))}</span>
      <button class="btn sm" data-act="preview" data-pid="${h(pid)}" type="button">Preview edit</button></div>` : ''}</div>`;
}
function selectField(f) {
  S.mfield = f;
  $$('.fields button').forEach(b => { const on = b.dataset.field === f; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); });
  const hl = f && regionOf(f);
  $$('.xs .rg').forEach(r => r.classList.toggle('hl', r.dataset.region === hl));
  const fi = $('#fieldInfo'); if (fi) fi.innerHTML = fieldInfo();
  if (S.panel === 'input' && S.mctx) {
    const bar = $('.fsel'), html = fselHtml(f);
    if (bar) bar.outerHTML = html;
    else if (html) { const t = $('.ptools'); if (t) t.insertAdjacentHTML('afterend', html); }
  }
  S.panelKey = panelKey();
  afterModel();
}
function latticeXs(m, sel) {
  const g = m.geometry, n = g.layout.length, W = 280, k = W / g.assembly_pitch_cm, off = (g.assembly_pitch_cm - n * g.rod_pitch_cm) / 2 * k, p = g.rod_pitch_cm * k;
  const hl = sel && regionOf(sel);
  const col = {fuel: 'var(--m-fuel)', gap: 'var(--m-gap)', clad: 'var(--m-clad)', moderator: 'var(--m-water)'};
  const circles = g.layout.map((row, r) => row.split('').map((ch, c) => [...g.pins[ch]].reverse().map(ring =>
    `<circle class="rg ${hl === ring.region ? 'hl' : ''}" cx="${(off + (c + 0.5) * p).toFixed(2)}" cy="${(off + (r + 0.5) * p).toFixed(2)}" r="${(ring.outer_radius_cm * k).toFixed(2)}" fill="${col[ring.region]}" data-region="${ring.region}"/>`).join('')).join('')).join('');
  return `<svg class="xs" viewBox="0 0 ${W} ${W}" role="img" aria-label="${h(m.case)} xy cross section: ${n} × ${n} lattice">
    <rect class="rg ${hl === 'moderator' ? 'hl' : ''}" x="0.5" y="0.5" width="${W - 1}" height="${W - 1}" fill="${col.moderator}" data-region="moderator"/>${circles}
    </svg>`;
}
function xsSvg(m, sel) {
  if (!m) return '';
  if (m.geometry.kind === 'lattice') return latticeXs(m, sel);
  const P = m.geometry.pitch_cm, W = 280, k = W / P, c = W / 2;
  const hl = sel && regionOf(sel);
  const col = {fuel: 'var(--m-fuel)', gap: 'var(--m-gap)', clad: 'var(--m-clad)', moderator: 'var(--m-water)'};
  const rings = [...m.geometry.rings].reverse();
  const bnd = m.geometry.radial_boundary === 'reflective' ? 'Reflective BC' : 'Vacuum BC';
  return `<svg class="xs" viewBox="0 0 ${W} ${W}" role="img" aria-label="${h(m.case)} xy cross section: ${h(m.geometry.rings.map(r => `${LABEL[r.region]} r = ${r.outer_radius_cm} cm`).join(', '))}, pitch ${P} cm, ${bnd}">
    <rect class="rg ${hl === 'moderator' ? 'hl' : ''}" x="0.5" y="0.5" width="${W - 1}" height="${W - 1}" fill="${col.moderator}" data-region="moderator"><title>Moderator, pitch ${P} cm, ${bnd}</title></rect>
    ${rings.map(r => `<circle class="rg ${hl === r.region ? 'hl' : ''}" cx="${c}" cy="${c}" r="${r.outer_radius_cm * k}" fill="${col[r.region] || '#ccc'}" data-region="${r.region}"><title>${LABEL[r.region]}, outer radius ${r.outer_radius_cm} cm</title></circle>`).join('')}
    <text x="8" y="16">Pitch ${P} cm</text><text x="8" y="${W - 8}">${bnd}</text></svg>`;
}

/* ---------- Input ---------- */
async function viewInput() {
  const {c, on, cn} = await loadModelCtx();
  const n = S.msolver === 'ref_mc' ? cn : on;
  const rb = s => (c['readback.' + s] || {}).status || 'none';
  const sel = S.mfield;
  return `<div class="ptools">${casePick()}
      <div class="seg seg-s" role="group" aria-label="Solver">${['openmc', 'ref_mc'].map(s => `<button type="button" data-solver="${s}" aria-pressed="${S.msolver === s}">${vglyph(rb(s))}${SOLVER[s]}</button>`).join('')}</div></div>
    ${fselHtml(sel)}
    ${n ? `<div class="code" data-code="${S.msolver}"><div class="code-h mono">${h(n.meta.filename)}</div>
      <pre>${n.text.replace(/\n$/, '').split('\n').map((l, i) => `<span class="ln" data-n="${i + 1}">${h(l) || ' '}</span>`).join('')}</pre></div>` : ''}`;
}
function fselHtml(sel) {
  return sel ? `<div class="fsel"><span>${h(fieldLabel(sel, S.mctx.pl) || sel)}</span><span class="num">${h(showVal(S.mctx.fields[sel]))}</span>
      <button class="ib sm" type="button" data-field="${h(sel)}" aria-label="Clear selection" title="Clear selection">${ic('close')}</button></div>` : '';
}
async function afterModel() {
  const sel = S.mfield, mc = S.mctx;
  $$('.code pre .ln.hl').forEach(e => e.classList.remove('hl'));
  if (sel && mc && S.panel === 'input') {
    const n = S.msolver === 'ref_mc' ? mc.cn : mc.on;
    const pre = $('.code pre');
    const lines = (n && n.line_map[sel]) || [];
    lines.forEach(i => { const e = pre && pre.children[i - 1]; if (e) e.classList.add('hl'); });
    const first = pre && lines.length && pre.children[lines[0] - 1];
    if (first) pre.scrollTop = Math.max(0, first.offsetTop - (pre.clientHeight - first.offsetHeight) / 2);
  }
  if (S.plotOn && S.panel === 'model') {
    const c = S.snap.cases.find(x => x.case === S.mcase);
    const img = $('#omcPlot');
    if (img && c && c.geomcheck) {
      if (S.plots[c.geomcheck.id] === undefined) S.plots[c.geomcheck.id] = (await api(`api/evidence/${c.geomcheck.id}`)).detail_json.plot || null;
      const p = S.plots[c.geomcheck.id];
      if (p) img.src = `api/blob/${p}`;
    }
  }
}
/* Map clicked solver input line number back to its physics field */
function fieldAtLine(n) {
  const mc = S.mctx;
  const f = mc && (S.msolver === 'ref_mc' ? mc.cn : mc.on);
  if (!f) return null;
  const hit = Object.entries(f.line_map || {}).filter(([, ls]) => ls.includes(n)).map(([k]) => k).filter(k => fieldLabel(k, mc.pl));
  return hit[0] || null;
}

/* ---------- Evidence ---------- */
const EVK = {rules: 'Rules', readback: 'Readback', geomcheck: 'Geometry check', run: 'Run', acceptance: 'Acceptance', precheck: 'Precheck', confirmation: 'Confirmation',
  override: 'Override', generate: 'Generate', check: 'Precheck step'};
async function viewEvidence() {
  if (!S.evidence.length) S.evidence = await api(`api/evidence?goal=${S.gid}`);
  if (!S.evSel) {
    const list = [...S.evidence].reverse().filter(e => e.current || e.status !== 'running');
    return `<ul class="evlist">${list.map(e => `<li><button type="button" data-evsel="${e.id}">${vglyph(e.status)}
      <span class="ev-k">${h(e.case_id || 'Goal')} · ${h(EVK[e.kind] || e.kind)}</span><span class="ev-s">${h(e.summary || '')}</span></button></li>`).join('')}</ul>`;
  }
  if (!S.evDetail || S.evDetail.id !== S.evSel) S.evDetail = await api(`api/evidence/${S.evSel}`);
  return `<button class="btn ghost sm" type="button" data-evsel="0">${ic('back')}Evidence</button>${await evidenceDetail(S.evDetail)}`;
}
function latestFor(e) {
  if (e.current) return null;
  return S.evidence.find(x => x.current && x.kind === e.kind && x.case_id === e.case_id) || null;
}
async function evidenceDetail(e) {
  const d = e.detail_json || {};
  const lat = latestFor(e);
  let body = '';
  if (e.kind === 'rules') {
    body = `<table class="tbl"><thead><tr><th>Rule</th><th><span class="sr">Verdict</span></th><th>Detail</th></tr></thead><tbody>${(d.results || []).map(r => `<tr><td>${h(r.rule)} ${h(r.name)}</td><td>${vglyph(r.status)}</td><td>${h(r.message)}</td></tr>`).join('')}</tbody></table>`;
  } else if (e.kind === 'readback') {
    const bad = d.mismatches || [];
    const cell = v => h(typeof v === 'number' ? +v.toPrecision(8) : v == null ? 'missing' : typeof v === 'string' ? showVal(v) : JSON.stringify(v));
    body = `<div class="tblw"><table class="tbl"><thead><tr><th>Field</th><th>Model</th><th>Readback</th><th>Lines</th></tr></thead><tbody>
      ${[...bad, ...(d.rows || []).filter(r => r.status === 'same')].slice(0, 80).map(r => `<tr class="${r.status !== 'same' ? 'bad' : ''}"><td>${h(fieldLabel(r.path) || r.path)}</td><td class="num">${cell(r.confirmed)}</td><td class="num">${cell(r.readback)}</td><td class="num">${h((r.lines || []).join(' '))}</td></tr>`).join('')}
      </tbody></table></div>
      <h4>Bindings</h4><div class="tblw"><table class="tbl"><tbody>${(d.binding_rows || []).map(r => `<tr class="${r.status !== 'same' ? 'bad' : ''}"><td>${h(r.path)}</td><td class="mono sm">${h(typeof r.confirmed === 'object' ? Object.values(r.confirmed || {}).join(' ') : r.confirmed)}</td><td>${r.status === 'same' ? glyph('pass', 'Match') : glyph('fail', 'Mismatch')}</td></tr>`).join('')}</tbody></table></div>
      ${(d.notes || []).length ? `<ul class="notes">${d.notes.map(n => `<li>${h(n)}</li>`).join('')}</ul>` : ''}
      ${(d.uninterpreted || []).length ? `<p class="mut">Uninterpreted: ${h(d.uninterpreted.map(u => u.card ? `${u.card} (line ${u.line})` : u).join(', '))}</p>` : ''}`;
  } else if (e.kind === 'geomcheck') {
    body = `${d.plot ? `<img class="plot" src="api/blob/${d.plot}" alt="OpenMC xy cross section">` : ''}<pre class="logt">${h((d.log_tail || []).join('\n'))}</pre>`;
  } else if (e.kind === 'run') {
    const r = (await node(d.run_node)).content;
    body = `<dl class="kv"><dt>k</dt><dd class="num">${fmt(r.result.k)} ± ${fmt(r.result.sigma)} (${h(r.result.estimator)})</dd>
      <dt>Rel. err</dt><dd class="num">${(r.result.rel_err * 100).toFixed(4)}%</dd>
      ${Object.entries(r.result.per_estimator || {}).map(([k, v]) => `<dt>${h(k)}</dt><dd class="num">${fmt(v.k)} ± ${fmt(v.sigma)}</dd>`).join('')}
      <dt>Particles</dt><dd class="num">${r.result.particles} × ${r.result.batches}, ${r.result.inactive} inactive</dd>
      <dt>Solver</dt><dd>OpenMC ${h(r.identity.version)}, ${h(r.identity.library)}</dd>
      <dt>Input</dt><dd class="mono sm">${h(r.identity.solver_input)}</dd><dt>Binding</dt><dd class="mono sm">${h(r.identity.binding)}</dd>
      <dt>Runtime</dt><dd>${dur(r.wall_s)}, ${r.threads} threads</dd>
      <dt>Attempt</dt><dd>#${r.attempt}${r.restart_from ? `, resumed from ${h(r.restart_from.split('/').pop())}` : ''}${d.reused ? ', cached' : ''}</dd></dl>`;
  } else if (e.kind === 'acceptance') {
    const ev = d.evaluation;
    body = `<dl class="kv"><dt>Δ</dt><dd class="num">${pcm(ev.delta_pcm)} pcm</dd><dt>σ_c</dt><dd class="num">${ev.sigma_c_pcm.toFixed(1)} pcm</dd><dt>Verdict</dt><dd>${h(ev.note)}</dd></dl>
      ${d.investigation && d.investigation.scale.length ? `<h4>Sensitivity & Misreading Scale</h4><div class="tblw"><table class="tbl"><tbody>${d.investigation.scale.map(s => `<tr><td>${h(s.id)}</td><td>${h(s.what)}</td><td class="num">${pcm(s.dk_pcm)} ± ${s.sigma_pcm.toFixed(0)}</td></tr>`).join('')}</tbody></table></div><p class="mut">${h(d.investigation.scale_source)}</p>` : ''}`;
  } else {
    body = `<pre class="logt">${h(JSON.stringify(d, null, 1).slice(0, 6000))}</pre>`;
  }
  let subj = null;
  try { subj = await node(e.subject); } catch (_) { subj = null; }
  const cur = (S.evidence.find(x => x.id === e.id) || {}).current;
  return `<div class="evd"><h3>${vglyph(e.status)}${h(e.case_id || 'Goal')} · ${h(EVK[e.kind] || e.kind)}</h3>
    ${e.summary ? `<p>${h(e.summary)}</p>` : ''}
    <div class="acts in keep">${subj ? `<button class="btn sm" data-open="${h(e.subject)}" type="button">Artifact</button>` : ''}
      ${!cur && lat ? `<button class="btn sm" data-evsel="${lat.id}" type="button">Latest</button>` : ''}</div>
    ${body}<div id="artBox"></div></div>`;
}
async function openArtifact(hsh) {
  const n = await node(hsh);
  const box = $('#artBox');
  if (!box) return;
  const txt = n.text != null ? n.text : JSON.stringify(n.content, null, 1);
  box.innerHTML = `<h4 class="mono sm">${h(hsh.slice(0, 16))} · ${h(n.kind)}</h4><pre class="logt tall">${h(txt)}</pre>`;
}

/* ---------- Report ---------- */
function md2html(md) {
  const lines = md.split('\n'); let out = '', inT = false, inL = false, head = true;
  for (const l of lines) {
    if (l.startsWith('|')) {
      if (/^\|[-:| ]+\|$/.test(l)) continue;
      const cells = l.slice(1, -1).split('|').map(c => c.trim());
      const tag = head ? 'th' : 'td';
      out += (inT ? '' : '<div class="tblw"><table class="tbl">') + '<tr>' + cells.map(c => `<${tag}>${inlineMd(c)}</${tag}>`).join('') + '</tr>';
      inT = true; head = false; continue;
    } else if (inT) { out += '</table></div>'; inT = false; }
    head = true;
    if (l.startsWith('- ')) { out += (inL ? '' : '<ul>') + `<li>${inlineMd(l.slice(2))}</li>`; inL = true; continue; }
    else if (inL) { out += '</ul>'; inL = false; }
    if (l.startsWith('# ')) out += `<h1>${inlineMd(l.slice(2))}</h1>`;
    else if (l.startsWith('## ')) out += `<h2>${inlineMd(l.slice(3))}</h2>`;
    else if (l.startsWith('### ')) out += `<h3>${inlineMd(l.slice(4))}</h3>`;
    else if (l.trim()) out += `<p>${inlineMd(l)}</p>`;
  }
  return out + (inT ? '</table></div>' : '') + (inL ? '</ul>' : '');
}
async function viewReport() {
  const latest = S.snap.report && S.snap.report.hash;
  const hash = S.reportHash || latest;
  if (!hash) return ro() || S.snap.goal.status !== 'active' ? '' : `<div class="acts"><button class="btn" data-act="report" type="button">Generate report</button></div>`;
  const n = (await node(hash)).content;
  const ver = (n.facts && n.facts.goal || {}).version, cur = S.snap.goal.version;
  return `<article class="report">
    ${ver && ver !== cur ? `<p class="mut">Report for goal v${ver}; current version is v${cur}.</p>` : ''}
    ${ro() ? '' : `<div class="acts in r">${hash !== latest && latest ? `<button class="btn sm" type="button" data-report-open="${h(latest)}">Latest report</button>` : ''}
      <button class="btn sm" data-act="report" type="button">Regenerate</button></div>`}
    ${md2html(n.markdown)}
    ${n.narrative ? `<h2>Summary Notes</h2>${n.narrative.map(p => `<p>${h(p)}</p>`).join('')}` : ''}
  </article>`;
}
