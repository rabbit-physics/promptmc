'use strict';
/* Compute nodes: settings menu summary and node management dialog (online, state, resources, toggle, stop) */

const ND_STATUS = {idle: 'Idle', run: 'Running', suspend: 'Yielding', hold: 'Held', disabled: 'Disabled', offline: 'Offline'};
const ND_UNIT = {hist: 'hist/s', particle: 'part/s'};
const ND_SOLVERS = {openmc: 'OpenMC', ref_mc: 'Reference MC'};

let ndLoading = null;
async function loadNodes() {
  if (!ndLoading) ndLoading = (async () => {
    try { S.cn = await api('api/nodes'); S.cnErr = ''; }
    catch (e) { S.cnErr = e.message; }
    renderAvatar();
    return S.cn;
  })().finally(() => { ndLoading = null; });
  return ndLoading;
}
const ndList = () => (S.cn && S.cn.nodes) || [];
const ndState = n => S.cnErr ? 'Status unknown' : n.online ? `Online · ${ND_STATUS[n.status] || n.status}` : 'Offline';

function ndSolver(n, key) {
  const s = n.solvers && n.solvers[key];
  if (S.cnErr || !s) return {g: 'none', label: 'Unknown', why: S.cnErr ? 'Unable to refresh node status' : 'Solver registration not received'};
  if (!s.registered) return {g: 'bad', label: 'Unregistered', why: 'Solver not registered on this node'};
  if (!s.data_files) return {g: 'warn', label: 'No data', why: 'Executable registered, nuclear data manifest empty'};
  const why = `Registered executable and ${s.data_files} nuclear data files; jobs verify required digests`;
  if (!n.online) return {g: 'none', label: 'Offline', why};
  if (n.state === 'suspend' || n.status === 'suspend') return {g: 'paused', label: s.tasks ? 'Suspended' : n.enabled ? 'Held' : 'Disabled', why};
  if (s.tasks && n.host && n.host.suspended) return {g: 'warn', label: 'Claimed', why: `Node partially suspended; ${s.tasks} tasks using ${s.slots_used} slots`};
  if (s.tasks) return {g: 'run', label: 'Running', why: `${s.tasks} tasks, ${s.slots_used} slots; ${why}`};
  if (!n.enabled) return {g: 'paused', label: 'Disabled', why};
  if (n.status === 'hold') return {g: 'paused', label: 'Held', why};
  return {g: 'done', label: 'Ready', why};
}

function ndSolverCell(n, key) {
  const s = ndSolver(n, key);
  return `<span class="nd-sstate" title="${h(s.why)}">${glyph(s.g, s.label)}<span>${s.label}</span></span>`;
}

/* Summary row inside settings popover */
function nodesEnvRow() {
  const ns = ndList(), on = ns.filter(n => n.online).length;
  let g;
  if (S.cnErr) g = glyph('bad', S.cnErr);
  else if (!S.cn) g = glyph('none', 'Compute nodes');
  else if (ns.length && on === ns.length) g = glyph('done', 'All online');
  else if (on) g = glyph('warn', `${ns.length - on} offline`);
  else g = glyph('bad', 'No nodes online');
  return `<button class="env-r env-b" type="button" data-act="node-settings">${g}<span>Compute nodes</span><span class="mi-v num">${S.cn && !S.cnErr ? `${on}/${ns.length} online` : S.cnErr ? 'Error' : 'Loading'}</span>${ic('chev', 'mi-c')}</button>
    ${ns.length ? `<table class="env-nodes" aria-label="Per-node solver status"><thead><tr><th scope="col">Node</th><th scope="col">OpenMC</th><th scope="col">Reference MC</th></tr></thead>
      <tbody>${ns.map(n => `<tr><th scope="row"><span class="mono">${h(n.name)}</span><small>${h(ndState(n))}</small></th>${Object.keys(ND_SOLVERS).map(k => `<td>${ndSolverCell(n, k)}</td>`).join('')}</tr>`).join('')}</tbody></table>`
      : `<p class="nd-note">${S.cnErr ? 'Cannot reach compute node coordinator' : S.cn ? 'No compute nodes registered' : 'Loading compute nodes...'}</p>`}`;
}
/* Refresh open settings popover without moving focus */
const ndMenuHtml = new WeakMap();
async function refreshEnvMenu() {
  await loadNodes();
  const pop = $('#pop');
  if (pop.hidden || !popFor || popFor.id !== 'envBtn') return;
  const html = envMenu();
  if (ndMenuHtml.get(pop.firstElementChild) === html) return;
  const a = document.activeElement, key = a && pop.contains(a) ? (a.dataset.act ? `[data-act="${a.dataset.act}"]` : a.dataset.theme ? `[data-theme="${a.dataset.theme}"]` : '') : '';
  pop.innerHTML = html;
  ndMenuHtml.set(pop.firstElementChild, html);
  placePop();
  if (key) { const b = $(key, pop); if (b) b.focus({preventScroll: true}); }
}
setInterval(() => {
  if (!$('#pop').hidden && popFor && popFor.id === 'envBtn') refreshEnvMenu();
}, 5000);

/* ---------- Status Icons ---------- */
function ndGlyph(kind, label) {
  const t = h(label);
  const g = {
    suspend: `<circle cx="8" cy="8" r="7" fill="var(--warn)"/><rect x="5.4" y="4.8" width="1.8" height="6.4" rx=".5" fill="var(--on-ok)"/><rect x="8.8" y="4.8" width="1.8" height="6.4" rx=".5" fill="var(--on-ok)"/>`,
    ecore: `<rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="none" stroke="var(--gl-mute)" stroke-width="1.3"/><rect x="5.5" y="8.5" width="2.2" height="2.2" rx=".4" fill="var(--warn)"/><path d="M6 1.5v2M10 1.5v2M6 12.5v2M10 12.5v2M1.5 6h2M1.5 10h2M12.5 6h2M12.5 10h2" stroke="var(--gl-mute)" stroke-width="1.2" stroke-linecap="round"/>`,
    battery: `<rect x="1.5" y="4.5" width="11.5" height="7" rx="1.5" fill="none" stroke="var(--gl-mute)" stroke-width="1.3"/><rect x="13.5" y="6.5" width="1.5" height="3" rx=".5" fill="var(--gl-mute)"/><rect x="3" y="6" width="2.5" height="4" rx=".5" fill="var(--warn)"/>`,
  }[kind];
  return `<svg class="gl" width="16" height="16" viewBox="0 0 16 16" role="img" aria-label="${t}"><title>${t}</title>${g}</svg>`;
}
/* One square per slot: active slots filled in accent color, suspended slots in warning color */
function slotMeter(n) {
  const busy = n.slots_used != null ? n.slots_used : n.used || 0;
  const total = n.slots || 0, used = Math.min(busy, total || busy);
  const su = n.status === 'suspend' ? used : Math.min((n.host && n.host.suspended) || 0, used);
  const lab = `${ND_STATUS[n.status] || n.status} ${used}/${total} slots${su > 0 ? `, ${su} suspended` : ''}`;
  const cell = i => i >= used ? '<i></i>' : i >= used - su ? '<i class="on" style="background:var(--warn)"></i>' : '<i class="on"></i>';
  return `<span class="ndm${su && su === used ? ' su' : ''}" role="img" aria-label="${h(lab)}" title="${h(lab)}">${Array.from({length: Math.max(total, used)}, (_, i) => cell(i)).join('')}</span>`;
}
function statusGlyphs(n) {
  let s = '';
  if (n.online && n.status === 'suspend') s += ndGlyph('suspend', ND_STATUS.suspend);
  if (n.online && n.status === 'hold') s += glyph('paused', ND_STATUS.hold);
  if (n.reject) s += glyph('bad', n.reject);
  const ho = n.online && n.host;
  if (ho && ho.ecores) s += ndGlyph('ecore', 'High host CPU: pinned to E-cores');
  if (ho && ho.ac === false) s += ndGlyph('battery', 'On battery power');
  return s;
}
const ndGb = x => x ? `${(x / 2 ** 30).toFixed(1)} GB` : '—';
const ndK = x => x >= 1e6 ? `${(x / 1e6).toFixed(2)}M` : x >= 1e3 ? `${(x / 1e3).toFixed(1)}k` : `${Math.round(x)}`;
function ndResources(n) {
  const cores = n.cpus != null ? (Number.isInteger(n.cpus) ? n.cpus : +n.cpus.toFixed(1)) : n.ncpu;
  const load = n.load1, base = n.ncpu || n.cpus || 1;
  const lf = load == null ? 0 : Math.min(1, load / base);
  const r = n.rate;
  return `<span class="ndv num" title="Available cores">${cores != null ? `${cores} cores` : '—'}</span>
    <span class="ndv num" title="Memory limit">${ndGb(n.mem_bytes)}</span>
    <span class="ndv ndl1" title="${load == null ? 'Load' : `1-min load ${load.toFixed(2)} / ${base} cores`}"><i class="bar ${lf > .85 ? 'hot' : ''}"><b style="width:${(lf * 100).toFixed(0)}%"></b></i><span class="num">${load == null ? '—' : load.toFixed(1)}</span></span>
    <span class="ndv num" title="Recent measured throughput${r && r.job ? ` (job ${h(r.job)})` : ''}">${r && r.per_s ? `${h(ND_SOLVERS[r.solver] || r.solver || '')} ${ndK(r.per_s)} ${ND_UNIT[r.unit] || '/s'}` : '—'}</span>`;
}
function ndSolvers(n) {
  return `<div class="nd-solvers">${Object.entries(ND_SOLVERS).map(([key, label]) => {
    const s = n.solvers && n.solvers[key];
    return `<div class="nd-solver"><div><b>${label}</b>${ndSolverCell(n, key)}</div>
      <small>${s && s.registered ? `${s.data_files} data files${!S.cnErr && n.online ? ` · ${s.tasks} tasks / ${s.slots_used} slots` : ' · last seen'}` : 'Not registered'}</small></div>`;
  }).join('')}</div>`;
}
function ndRow(n) {
  const sure = S.ndSure === n.name;
  const stop = sure ? `<button class="btn sm danger" type="button" data-nd-stop="${h(n.name)}">Confirm stop</button>`
    : `<button class="ib sm" type="button" data-nd-stop="${h(n.name)}" aria-label="Stop ${h(n.name)} now" title="Stop now" ${n.used ? '' : 'disabled'}>${icS('stop')}</button>`;
  return `<li class="ndr ${n.online && !S.cnErr ? '' : 'off'} ${n.enabled ? '' : 'dis'}" data-nd-row="${h(n.name)}">
    <div class="nd1"><i class="gd ${n.online && !S.cnErr ? 'ok' : ''}" role="img" aria-label="${h(ndState(n))}" title="${h(ndState(n))}"></i>
      <span class="ndn mono">${h(n.name)}</span>${slotMeter(n)}<span class="nds">${statusGlyphs(n)}</span><span class="grow"></span>${stop}
      <button class="swb" type="button" role="switch" aria-checked="${n.enabled}" aria-label="Enable ${h(n.name)}" title="Enable" data-nd-en="${h(n.name)}"><i class="sw" aria-hidden="true"></i></button></div>
    <div class="nd2 nd-summary"><span>${h(ndState(n))}</span><span class="num">${S.cnErr || !n.online ? '—' : n.slots_used || 0}/${n.slots || 0} slots</span><span title="Last node heartbeat">${S.cnErr ? 'Update failed' : n.seen_s == null ? 'Never connected' : `Heartbeat ${Math.max(0, Math.floor(n.seen_s))}s ago`}</span></div>
    <div class="nd2">${ndResources(n)}</div>${ndSolvers(n)}</li>`;
}
function renderNd() {
  const dlg = $('#nodeDlg');
  const a = document.activeElement, fk = a && dlg.contains(a) ? (a.dataset.ndEn ? `[data-nd-en="${a.dataset.ndEn}"]` : a.dataset.ndStop ? `[data-nd-stop="${a.dataset.ndStop}"]` : a.dataset.nd ? `[data-nd="${a.dataset.nd}"]` : '') : '';
  const ns = ndList();
  const body = `${S.cnErr ? `<p class="derr" role="alert">${h(S.cnErr)}</p>` : ''}<ul class="ndls">${ns.map(ndRow).join('')}</ul>
    <p class="nd-note">${ns.length ? 'Refreshes every 5 s. Nuclear data manifests are registered per node and verified at dispatch.' : S.cnErr ? 'Unable to query compute nodes' : 'No compute nodes registered'}</p>`;
  const b = $('.nd-b', dlg);
  if (!b) {
    dlg.innerHTML = `<div class="nd-w"><header class="nd-h"><h2 id="nodeT">Compute nodes</h2>
      <button class="ib" type="button" data-nd="close" aria-label="Close" title="Close">${ic('close')}</button></header><div class="nd-b">${body}</div></div>`;
  } else if (b.dataset.h !== body) {
    b.innerHTML = body;
  }
  $('.nd-b', dlg).dataset.h = body;
  if (fk) { const f = $(fk, dlg); if (f && !f.disabled) f.focus({preventScroll: true}); }
}
let ndTimer = null;
async function openNodeDialog() {
  S.ndSure = null;
  const dlg = $('#nodeDlg');
  dlg.innerHTML = '';
  await loadNodes();
  renderNd();
  dlg.showModal();
  clearInterval(ndTimer);
  ndTimer = setInterval(async () => { await loadNodes(); renderNd(); }, 5000);
}
$('#nodeDlg').addEventListener('close', () => { clearInterval(ndTimer); ndTimer = null; S.ndSure = null; });
async function ndToggle(name) {
  S.ndSure = null;
  const n = ndList().find(x => x.name === name);
  if (!n) return;
  const want = !n.enabled;
  n.enabled = want;
  if (n.online && !want) n.status = 'disabled';
  renderNd();
  try { await api('api/nodes/enable', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({node: name, enabled: want})}); }
  catch (e) { toast(e.message); }
  await loadNodes(); renderNd();
}
/* Two-step confirmation for node stop: resets after 4 seconds */
let ndSureTimer = null;
async function ndStop(name) {
  clearTimeout(ndSureTimer);
  if (S.ndSure !== name) {
    S.ndSure = name; renderNd();
    const b = $(`#nodeDlg [data-nd-stop="${name}"]`); if (b) b.focus({preventScroll: true});
    ndSureTimer = setTimeout(() => { if (S.ndSure === name) { S.ndSure = null; renderNd(); } }, 4000);
    return;
  }
  S.ndSure = null;
  try { await api('api/nodes/stop', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({node: name})}); }
  catch (e) { toast(e.message); }
  await loadNodes(); renderNd();
  const sw = $(`#nodeDlg [data-nd-en="${name}"]`); if (sw) sw.focus({preventScroll: true});
}
