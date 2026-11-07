'use strict';
/* Application shell: routing, sidebar, topbar, composer, SSE updates, and event handlers */

/* ---------- Data Loading ---------- */
async function loadGoals() {
  try { [S.goals, S.talks] = await Promise.all([api('api/goals'), api('api/talks')]); } catch (e) { toast(e.message); }
}
async function loadTalk(tid) {
  const t = await api(`api/talk?id=${encodeURIComponent(tid)}`);
  S.temp = t.messages.map(m => ({role: m.role, text: m.text, docs: m.meta.docs, goal: m.meta.goal}));
}
function listState(g) {
  if (g.id === S.gid && S.snap) return goalState(S.snap);
  return g.state || {k: 'prep', l: 'Preparing & prechecking'};
}
async function refresh(full = true) {
  if (!S.gid) { renderAll(); return; }
  const gid = S.gid;
  try {
    const sn = await api(`api/state?goal=${gid}`);
    if (gid !== S.gid) return;
    S.snap = sn;
    noticeChecks();
    if (S.panel === 'evidence') S.evidence = await api(`api/evidence?goal=${gid}`);
  } catch (e) { toast(e.message); }
  if (full) renderAll(); else { renderSide(); renderTop(); renderDock(); renderThread(); renderPanel(false); }
}
let softTimer = null;
function softRefresh() { clearTimeout(softTimer); softTimer = setTimeout(() => refresh(false), 250); }
let goalsTimer = null;
function softGoals(gid) {
  clearTimeout(goalsTimer);
  goalsTimer = setTimeout(async () => {
    await loadGoals();
    renderSide();
  }, 400);
}

let es = null;
function connect() {
  if (es) es.close();
  es = new EventSource(`events?after=${S.snap ? S.snap.last_seq : (S.health && S.health.last_seq) || 0}`);
  es.addEventListener('notification', ev => {
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (['goal/stateChanged', 'report/published', 'task/stateChanged', 'task/completed'].includes(m.method)) softGoals(m.goal);
    if (!m.goal || m.goal !== S.gid) return;
    if (m.method === 'run/progress' && m.params.trial) {
      S.progress[`trial:${m.params.trial}`] = m.params;
      const cell = $(`[data-tprog="${m.params.trial}"]`);
      if (cell && S.snap) cell.innerHTML = trialGlyph(propOf(m.params.trial));
      return;
    }
    if (m.method === 'run/progress' && m.params.solver === 'ref_mc') {
      S.progress[`refmc:${m.params.case}`] = m.params;
      const cell = $(`[data-rprog="${m.params.case}"]`);
      if (cell && S.snap) { cell.innerHTML = refMcCell(m.params.case); return; }
    }
    if (m.method === 'run/progress' && m.params.solver !== 'ref_mc') {
      S.progress[m.params.case] = m.params;
      const cell = $(`[data-prog="${m.params.case}"]`);
      if (cell && S.snap) { cell.innerHTML = progCell(m.params.case); return; }
    }
    if (m.method === 'output/textDelta') return m.params && m.params.call ? onThinkDelta(m.params) : softRefresh();
    softRefresh();
  });
  es.onerror = () => { es.close(); setTimeout(connect, 3000); };
}

/* ---------- Routing: #g=<goal>&p=<panel>&c=<case> ---------- */
function routeUrl(r) {
  const q = new URLSearchParams();
  if (r.t) q.set('t', r.t);
  if (r.g) q.set('g', r.g);
  if (r.g && r.p) q.set('p', r.p);
  if (r.g && r.c && (r.p === 'model' || r.p === 'input')) q.set('c', r.c);
  const s = q.toString();
  return s ? '#' + s : location.pathname + location.search;
}
function go(r, replace = false) {
  const url = routeUrl(r);
  const cur = location.hash ? location.hash : location.pathname + location.search;
  if (url !== cur) history[replace ? 'replaceState' : 'pushState'](null, '', url);
  return applyRoute();
}
let routeChain = Promise.resolve();
function applyRoute() { routeChain = routeChain.then(applyRouteNow, applyRouteNow); return routeChain; }
async function applyRouteNow() {
  const q = new URLSearchParams(location.hash.slice(1));
  const g = q.get('g') || q.get('goal') || null;
  const t = g ? null : q.get('t');
  let p = q.get('p') || q.get('tab');
  if (!PANELS.some(x => x[0] === p)) p = null;
  if (g !== S.gid || t !== S.tid || !S.routed) {
    if (document.activeElement && document.activeElement.dataset.val) document.activeElement.blur();
    if (Object.keys(S.rv.draft).length) toast('Unsaved mixture edits discarded');
    S.routed = true;
    if (g && !S.goals.some(x => x.id === g)) await loadGoals();
    if (g && !S.goals.some(x => x.id === g)) { toast(`Unknown goal ${g}`); S.routed = false; history.replaceState(null, '', location.pathname); return applyRouteNow(); }
    openGoal(g);
    if (t) {
      try { await loadTalk(t); S.tid = t; }
      catch (e) { toast(`Unknown talk ${t}`); S.routed = false; history.replaceState(null, '', location.pathname); return applyRouteNow(); }
    }
    if (q.get('c') || q.get('case')) S.mcase = q.get('c') || q.get('case');
    S.panel = g ? p : null;
    $('#app').classList.remove('nav-open');
    if (g) { renderAll(); await refresh(); } else renderAll();
    S.toEnd = true;
    if (t) renderThread();
    connect();
    if (!g) setTimeout(() => $('#msg').focus(), 0);
    return;
  }
  if (q.get('c')) S.mcase = q.get('c');
  if (p === 'evidence' && S.panel !== 'evidence') { try { S.evidence = await api(`api/evidence?goal=${S.gid}`); } catch (_) {} }
  S.panel = g ? p : null;
  if (S.panel) S.lastPanel = S.panel;
  renderPanelShell();
  renderTop();
  if (S.panel) {
    await renderPanel(true);
    if (S.panel === 'params' && !$('#pbody').classList.contains('split')) { $('#pbody').scrollTop = S.rv.scroll; S.rv.scroll = 0; } else $('#pbody').scrollTop = 0;
  }
}
function openGoal(gid) {
  Object.assign(S, {gid, tid: null, snap: null, evidence: [], evSel: null, evDetail: null, preview: null, mfield: null, previews: {},
    progress: {}, temp: null, pending: null, seenSteps: new Set(), seenReady: false, reportHash: null, pendingReading: {},
    threadHtml: null, toEnd: true, panelKey: null, dvals: {}, open: {}, src: null, rv: newRv(), rvHtml: null, srcKey: null});
}

/* ---------- Rendering ---------- */
function renderAll() { renderSide(); renderTop(); renderDock(); renderPanelShell(); renderThread(); if (S.panel) renderPanel(true); }

function renderSide() {
  const q = S.search.trim().toLowerCase();
  const match = g => !q || String(g.title).toLowerCase().includes(q);
  const all = [...S.goals, ...S.talks.map(t => Object.assign({talk: true}, t))].sort((a, b) => b.created - a.created);
  const act = all.filter(g => !g.archived && match(g));
  const arch = all.filter(g => g.archived && match(g));
  const showArch = S.showArchived || !!q || arch.some(g => g.id === S.gid || g.id === S.tid);
  const item = g => {
    if (g.talk) return `<a class="gi ${g.id === S.tid ? 'on' : ''}" href="#t=${h(g.id)}" data-talk="${h(g.id)}"${g.id === S.tid ? ' aria-current="page"' : ''}>${ic('talk', 'gic')}<span class="gt">${h(g.title)}</span></a>`;
    const st = listState(g);
    return `<a class="gi ${g.id === S.gid ? 'on' : ''}" href="#g=${h(g.id)}" data-goal="${h(g.id)}"${g.id === S.gid ? ' aria-current="page"' : ''}>${glyph(st.k, st.l, st.f)}<span class="gt">${h(g.title)}</span></a>`;
  };
  const html = act.map(item).join('')
    + (arch.length ? `<button class="grp-h" type="button" data-act="toggle-arch" aria-expanded="${showArch}">${ic('chev', 'tw')}<span>Archived</span></button>
      ${showArch ? `<div class="grp">${arch.map(item).join('')}</div>` : ''}` : '');
  const key = html + S.gid + S.tid;
  if (key === S.sideKey) return;
  S.sideKey = key;
  $('#glist').innerHTML = html;
}

function renderTop() {
  const sn = S.snap;
  const left = `<button class="ib narrow-only" type="button" data-act="nav" aria-label="Open sidebar" title="Open sidebar">${ic('menu')}</button>
    <button class="ib off-only" type="button" data-act="side-on" aria-label="Expand sidebar" title="Expand sidebar">${ic('side')}</button>
    <button class="ib off-only" type="button" data-act="new" aria-label="New chat" title="New chat">${ic('pen')}</button>`;
  const right = S.tid ? `<button class="ib" type="button" data-act="more" aria-label="More" title="More" aria-haspopup="true">${ic('more')}</button>`
    : sn ? `<button class="ib" type="button" data-act="panel" aria-label="Workspace" title="Workspace" aria-pressed="${!!S.panel}">${ic('panel')}</button>
    <button class="ib" type="button" data-act="more" aria-label="More" title="More" aria-haspopup="true">${ic('more')}</button>` : '';
  const m = curMode(), home = !S.gid && !S.tid && !S.temp && !S.pending;
  const seg = home ? `<div class="modes" role="group" aria-label="Mode">${[['chat', 'Chat'], ['work', 'Work']].map(([k, l]) =>
    `<button type="button" data-mode="${k}" aria-pressed="${m === k}">${l}</button>`).join('')}</div>` : '';
  const title = sn ? sn.goal.title : S.tid ? (S.talks.find(x => x.id === S.tid) || {}).title : '';
  const html = `<div class="top-l">${left}</div>${title ? `<h1 class="sr">${h(title)}</h1>` : ''}${seg}<div class="top-r">${right}</div>`;
  if (html !== S.topHtml) { $('#top').innerHTML = html; S.topHtml = html; }
  document.title = title ? `${title} - PromptMC-X` : 'PromptMC-X';
}

function renderThread() {
  const sc = $('#scroll'), th = $('#thread');
  const atEnd = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 48;
  const html = threadHtml();
  if (S.snap) S.seenReady = true;
  if (html !== S.threadHtml) {
    const held = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.cell;
    th.innerHTML = html;
    S.threadHtml = html;
    const back = held && th.querySelector(`[data-cell="${held}"]`);
    if (back) back.focus({preventScroll: true});
    if (atEnd || S.toEnd) sc.scrollTop = sc.scrollHeight;
  }
  if (S.toEnd && S.snap) { sc.scrollTop = sc.scrollHeight; S.toEnd = false; }
  $('#app').classList.toggle('ro', ro());
  maybeWorkDialog();
}

function goalMode(sn) {
  const g = sn.goal, m = g.spec && g.spec.mode;
  return m === 'chat' || m === 'work' ? m : ['active', 'blocked'].includes(g.status) ? 'work' : 'chat';
}
function curMode() { return S.snap ? goalMode(S.snap) : S.newMode; }
const POLICY = {goal: 'Auto', ask: 'Approve each'};
const BUDGET_H = [3, 12, 24, 48, 72];
function curPolicy() { return S.snap ? (S.snap.goal.mode === 'ask' ? 'ask' : 'goal') : S.newPolicy; }
function curBudget() { return S.snap && S.snap.budget ? S.snap.budget.limits.wall_s : null; }
function policyHtml() {
  const b = curBudget();
  return `<span class="mp-m">${POLICY[curPolicy()]}</span>${b ? `<span class="mp-b">${dur(b)}</span>` : ''}${ic('down')}`;
}
function policyLabel() {
  const b = curBudget();
  return `Policy: ${POLICY[curPolicy()]}` + (b ? `; time budget: ${dur(b)}` : '');
}
function renderDock() {
  const home = !S.gid && !S.tid && !S.temp && !S.pending;
  $('#main').classList.toggle('home', home);
  $('.hero').textContent = curMode() === 'work' ? 'What would you like to compute?' : 'Ask anything';
  const r = ro();
  $('#composer').hidden = r;
  $('#roBar').hidden = !r;
  $('#plusBtn').innerHTML = ic('plus');
  renderAtts();
  const pc = $('#presetChip');
  pc.hidden = !!S.snap || !S.newPreset;
  pc.textContent = presetLabel(S.newPreset);
  pc.setAttribute('aria-label', `Particle preset: ${presetLabel(S.newPreset)}`);
  if (home !== S.wasHome) { S.wasHome = home; growInput(); }
  renderPick();
  renderPolicy();
  renderSend();
}
function renderPolicy() {
  const b = $('#ppick');
  b.hidden = curMode() !== 'work';
  if (b.hidden) return;
  const html = policyHtml();
  if (b.dataset.h !== html) { b.innerHTML = html; b.dataset.h = html; }
  b.disabled = ro();
  b.setAttribute('aria-label', policyLabel());
}
function policyMenu() {
  const p = curPolicy(), b = curBudget();
  let html = `<div class="menu" role="menu" aria-label="Approval policy and time budget">${b ? '<div class="menu-h">Approval policy</div>' : ''}${Object.entries(POLICY).map(([k, l]) =>
    `<button type="button" role="menuitemradio" aria-checked="${k === p}" data-policy="${k}"><span>${l}</span>${k === p ? ic('check', 'mi-c') : ''}</button>`).join('')}`;
  if (b) {
    const used = S.snap.budget.used.wall_s || 0, hs = [...new Set([...BUDGET_H, b / 3600])].sort((x, y) => x - y);
    html += `<div class="menu-sep"></div><div class="menu-h">Time budget (used ${dur(used)})</div>` + hs.map(x =>
      `<button type="button" role="menuitemradio" aria-checked="${x * 3600 === b}" data-budget="${x}" ${ro() ? 'disabled' : ''}><span>${dur(x * 3600)}</span>${x * 3600 === b ? ic('check', 'mi-c') : ''}</button>`).join('');
  }
  return html + '</div>';
}
async function setPolicy(p) {
  if (!S.snap) {
    S.newPolicy = p;
    try { localStorage.setItem('p1-policy', p); } catch (_) {}
    renderDock();
    return;
  }
  if (p === curPolicy() || ro()) return;
  return act(() => rpc('task/submit', {goal: S.gid, kind: 'policy', input: {policy: p}}));
}
function renderSend() {
  const sn = S.snap, txt = $('#msg').value.trim();
  const going = x => x && ['running', 'queued', 'stopping'].includes(x.status);
  const busy = sn && (sn.cases.some(c => going(c.task) || going(c.ref_mc))
    || (sn.proposals || []).some(p => p.kind === 'trial' && going((p.trial || {}).task)));
  let html;
  if (sn && !txt && !ro() && goalMode(sn) === 'chat' && busy) {
    html = `<button class="cb round fill" type="button" data-act="stop" aria-label="Stop" title="Stop">${icS('stop')}</button>`;
  } else if (sn && !txt && !ro() && goalMode(sn) === 'work' && (busy || (sn.goal.status === 'active' && (sn.goal.paused || isRunning(sn))))) {
    html = (sn.goal.paused
      ? `<button class="cb round fill" type="button" data-act="continue" aria-label="Resume" title="Resume">${icS('play')}</button>`
      : `<button class="cb round fill" type="button" data-act="pause" aria-label="Pause" title="Pause">${icS('pause')}</button>`)
      + `<button class="cb round fill" type="button" data-act="stop" aria-label="Stop" title="Stop" ${busy ? '' : 'disabled'}>${icS('stop')}</button>`;
  } else {
    html = `<button class="cb round fill" type="submit" aria-label="Send" title="Send" ${(txt || attsReady()) && !attsBusy() && !S.pending ? '' : 'disabled'}>${ic('up')}</button>`;
  }
  const box = $('#sendBox');
  if (box.dataset.h !== html) { box.innerHTML = html; box.dataset.h = html; }
}
function growInput() {
  const ta = $('#msg');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
}

function renderPanelShell() {
  const on = !!(S.panel && S.snap);
  $('#panel').hidden = !on;
  $('#app').classList.toggle('panel-on', on);
}

/* ---------- Popover Menus ---------- */
let popFor = null;
function openPop(anchor, html, side = 'below') {
  const pop = $('#pop');
  if (popFor === anchor && !pop.hidden) { closePop(); return; }
  popFor = anchor;
  pop.innerHTML = html;
  pop.hidden = false;
  pop.dataset.side = side;
  placePop();
  anchor.setAttribute('aria-expanded', 'true');
  const f = pop.querySelector('[role="menuitemradio"][aria-checked="true"]:not([disabled])') || pop.querySelector('button:not([disabled])');
  if (f) f.focus({preventScroll: true});
}
function placePop() {
  const pop = $('#pop'), anchor = popFor, side = pop.dataset.side;
  if (!anchor || pop.hidden) return;
  const r = anchor.getBoundingClientRect(), pw = pop.offsetWidth, ph = pop.offsetHeight;
  let x = side === 'right-up' ? r.left : r.right - pw;
  if (side === 'left') x = r.left;
  x = Math.max(8, Math.min(x, innerWidth - pw - 8));
  let y = side === 'below' ? r.bottom + 6 : r.top - ph - 8;
  if (side !== 'below' && y < 8) y = r.bottom + 6;
  if (side === 'below' && y + ph > innerHeight - 8) y = r.top - ph - 6;
  pop.style.left = x + 'px'; pop.style.top = Math.max(8, Math.min(y, innerHeight - ph - 8)) + 'px';
}
function closePop() {
  const pop = $('#pop');
  if (pop.hidden) return;
  estStop();
  const back = pop.contains(document.activeElement) ? popFor : null;
  pop.hidden = true;
  if (popFor) { popFor.setAttribute('aria-expanded', 'false'); popFor = null; }
  if (back) back.focus({preventScroll: true});
}
/* ---------- + Menu: Attachments, Particle Presets & Runtime Estimates ---------- */
const curPreset = () => S.snap ? S.snap.goal.spec.run_preset : S.newPreset;
function presetMenu() {
  const cur = curPreset(), dis = !!S.snap && ro();
  const r = presetRun(cur) || PRESETS.verify;
  const keys = dis ? [cur] : Object.keys(PRESETS);
  const chips = keys.map(k => {
    const p = presetRun(k) || {};
    return `<button type="button" class="pchip ${PRESETS[k] ? 'mono' : 'num'}" role="menuitemradio" aria-checked="${k === cur}" data-preset="${h(k)}" ${dis ? 'disabled' : ''}
      title="${(p.particles || 0).toLocaleString('en')} particles/batch, ${p.batches} batches, ${p.inactive} inactive">${h(presetLabel(k))}</button>`;
  }).join('');
  const [pl, ph] = CUSTOM_RANGE.particles, [bl, bh] = CUSTOM_RANGE.batches;
  return `<div class="menu pmenu" role="menu" aria-label="Attach files and particle presets"><button type="button" role="menuitem" data-act="add-file">${ic('clip')}<span>Attach files</span></button>
    <div class="menu-sep"></div>
    <div class="menu-h pmh"><span>Particle preset</span><span class="pest" id="pest"></span></div>
    <div class="pchips" role="group" aria-label="Preset tiers">${chips}</div>
    ${dis ? '' : `<div class="pcustom" role="group" aria-label="Custom particles">
      <label for="pinP">Custom</label><input id="pinP" class="num" data-pin="p" inputmode="numeric" autocomplete="off" spellcheck="false" value="${r.particles}"
        title="Particles per batch, ${pl} to ${ph.toLocaleString('en')}" aria-label="Particles per batch">
      <span aria-hidden="true">×</span><input id="pinB" class="num" data-pin="b" inputmode="numeric" autocomplete="off" spellcheck="false" value="${r.batches}"
        title="Batches, ${bl} to ${bh}" aria-label="Batches"><span aria-hidden="true">batches</span>
      <button type="button" class="btn sm" data-act="preset-apply">Apply</button></div>`}</div>`;
}
function pinKey() {
  const num = id => { const v = ($(id) || {}).value || ''; const t = v.replace(/[,\s_]/g, ''); return /^\d+$/.test(t) ? +t : NaN; };
  return presetKey(num('#pinP'), num('#pinB'));
}
function pinCheck() {
  const [pl, ph] = CUSTOM_RANGE.particles, [bl, bh] = CUSTOM_RANGE.batches;
  const ok = (id, lo, hi) => { const t = (($(id) || {}).value || '').replace(/[,\s_]/g, ''); return /^\d+$/.test(t) && +t >= lo && +t <= hi; };
  [['#pinP', pl, ph], ['#pinB', bl, bh]].forEach(([id, lo, hi]) => {
    const el = $(id), good = ok(id, lo, hi);
    el.classList.toggle('bad', !good);
    if (good) el.removeAttribute('aria-invalid'); else el.setAttribute('aria-invalid', 'true');
  });
  const key = pinKey(), btn = $('#pop [data-act="preset-apply"]');
  if (btn) btn.disabled = !key;
  return key;
}
function setPreset(key, toggle) {
  closePop();
  if (!key) return;
  if (!S.snap) { S.newPreset = toggle && S.newPreset === key ? null : key; renderDock(); return; }
  if (ro() || key === S.snap.goal.spec.run_preset) return;
  return act(() => rpc('task/submit', {goal: S.gid, kind: 'rerun', input: {preset: key}}));
}
const EST_DEBOUNCE_MS = 300;
let estSeq = {all: 0, pin: 0}, estTimer = null, estRequest = 0;
function estOpen() {
  S.est = {map: {}, requests: {}, hover: null, pin: null};
  if (S.snap && ro()) return;
  S.est.pin = pinKey();
  estFetch('all', [...new Set([...Object.keys(PRESETS), S.est.pin].filter(Boolean))]);
}
function estStop() {
  clearTimeout(estTimer);
  estSeq.all++; estSeq.pin++;
  S.est = null;
}
async function estFetch(lane, keys) {
  const seq = ++estSeq[lane], gid = S.gid, E = S.est;
  if (!E) return;
  const request = ++estRequest;
  keys.forEach(key => { E.requests[key] = request; });
  renderEst();
  let r = null;
  try { r = await rpc('goal/estimate', gid ? {goal: gid, presets: keys} : {presets: keys}); } catch (_) {}
  if (seq !== estSeq[lane] || S.est !== E || gid !== S.gid) return;
  ((r && r.estimates) || []).forEach(e => { if (E.requests[e.preset] === request) E.map[e.preset] = e; });
  renderEst();
}
function estPin() {
  const E = S.est;
  if (!E) return;
  E.pin = pinCheck();
  clearTimeout(estTimer);
  if (E.pin && !E.map[E.pin]) estTimer = setTimeout(() => estFetch('pin', [E.pin]), EST_DEBOUNCE_MS);
  renderEst();
}
function estHover(key) {
  if (!S.est) return;
  S.est.hover = key;
  renderEst();
}
function estTip(e) {
  if (!e.cases) return 'All cases already have cached results for this preset';
  const what = e.per_case ? '1 case' : `${e.cases} cases`;
  if (e.how !== 'nodes') return `${what}, local serial estimate`;
  const n = e.nodes.length;
  return `${what}, ${n} node${n > 1 ? 's parallel' : ''} (${e.nodes.join(', ')})` + (e.local_cases ? `, ${e.local_cases} local serial fallback` : '');
}
function renderEst() {
  const el = $('#pest');
  if (!el) return;
  const E = S.est || {}, key = E.hover || E.pin, e = key && E.map && E.map[key];
  if (e) {
    el.innerHTML = e.cases ? h((e.per_case ? 'per case ' : '') + approx(e.wall_s)) : glyph('done-na', estTip(e));
    el.title = estTip(e);
    el.dataset.key = key;
  } else if (!key) {
    el.innerHTML = ''; el.title = ''; delete el.dataset.key;
  } else {
    el.textContent = '...'; el.title = ''; delete el.dataset.key;
  }
  el.classList.toggle('wait', !!key && !e);
}
function moreMenu() {
  const a = S.tid ? (S.talks.find(t => t.id === S.tid) || {}).archived : S.snap.goal.archived;
  return `<div class="menu" role="menu"><button type="button" role="menuitem" data-act="${a ? 'unarchive' : 'archive'}">${a ? 'Unarchive' : 'Archive'}</button></div>`;
}
function envMenu() {
  const hl = S.health || {}, llm = hl.llm || {};
  const th = document.documentElement.dataset.theme || 'system';
  return `<div class="menu env" role="dialog" aria-label="Compute nodes, models, and theme">
    ${nodesEnvRow()}
    <div class="menu-sep"></div>
    <button class="env-r env-b" type="button" data-act="llm-settings">${glyph(llm.available ? 'done' : 'bad', llm.available ? 'Available' : llm.reason || 'Unavailable')}<span>Model API</span><span class="mi-v">${h(curGroupName())}</span>${ic('chev', 'mi-c')}</button>
    ${S.snap ? `<div class="env-r ctx num">Context ${contextK()}</div>` : ''}
    <button class="env-r env-b" type="button" role="switch" aria-checked="${S.showThink}" data-act="think-toggle"><span>Show thinking</span><i class="sw" aria-hidden="true"></i></button>
    <div class="menu-sep"></div>
    <div class="seg seg-s theme" role="group" aria-label="Theme">
      ${[['system', 'sys', 'System'], ['light', 'sun', 'Light'], ['dark', 'moon', 'Dark']].map(([k, i, l]) => `<button type="button" data-theme="${k}" aria-pressed="${th === k}" aria-label="${l}" title="${l}">${ic(i)}</button>`).join('')}
    </div></div>`;
}
function renderAvatar() {
  const hl = S.health || {};
  const ns = ndList();
  const ready = ['openmc', 'ref_mc'].every(k => ns.some(n => n.online && n.enabled && n.solvers && n.solvers[k] && n.solvers[k].registered && n.solvers[k].data_files));
  const state = S.cnErr || (S.health && !(hl.llm && hl.llm.available)) ? 'bad'
    : !S.health || !S.cn ? '' : !ready ? 'bad' : ns.some(n => !n.online || !n.enabled || ['hold', 'suspend'].includes(n.status)) ? 'warn' : 'ok';
  $('#envBtn').innerHTML = `<svg width="20" height="20" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="8" cy="8" r="1.9" fill="currentColor"/></svg>
    <i class="hdot ${state}"></i>`;
}

/* ---------- Actions ---------- */
async function act(fn) {
  try { const r = await fn(); await refresh(false); return r; }
  catch (e) { toast(e.message); await refresh(false); }
}
/* ---------- Attachments ---------- */
let attSeq = 0;
function renderAtts() {
  const box = $('#atts');
  box.hidden = !S.atts.length;
  box.innerHTML = S.atts.map(a => `<span class="att ${a.err ? 'bad' : ''}" title="${h(a.err || a.name)}">
    ${a.hash ? (a.kind === 'image' ? `<img src="api/docs/${a.hash}/page/1.png" alt="">` : ic('doc')) : glyph('srun', 'Uploading')}
    <span class="att-n">${h(a.name)}</span>
    <button class="ib xs" type="button" data-att-rm="${a.key}" aria-label="Remove ${h(a.name)}" title="Remove">${ic('close')}</button></span>`).join('');
  renderSend();
}
async function addFiles(files) {
  const list = [...files];
  for (const file of list) {
    const name = file.name || `image-${Date.now() % 100000}.png`;
    const a = {key: ++attSeq, name};
    S.atts.push(a);
    renderAtts();
    try {
      const r = await fetch(`api/docs/upload?name=${encodeURIComponent(name)}`, {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `Upload failed (${r.status})`);
      if (S.atts.some(x => x.hash === j.hash)) { S.atts = S.atts.filter(x => x !== a); }
      else Object.assign(a, {hash: j.hash, kind: j.kind, blocked: j.blocked});
    } catch (e) {
      S.atts = S.atts.filter(x => x !== a);
      toast(`${name}: ${e.message}`);
    }
    renderAtts();
  }
}
const attsReady = () => S.atts.length > 0 && S.atts.every(a => a.hash);
const attsBusy = () => S.atts.some(a => !a.hash);
async function send() {
  const ta = $('#msg'), text = ta.value.trim();
  if ((!text && !attsReady()) || S.pending || attsBusy()) return;
  const docs = S.atts.map(a => a.hash), shown = S.atts.map(a => ({name: a.name, kind: a.kind}));
  S.atts = []; renderAtts();
  ta.value = ''; growInput();
  S.pending = text || ' '; S.pendingDocs = shown; S.toEnd = true;
  renderTop(); renderDock(); renderThread();
  if (!S.gid) {
    const temp = S.temp || [];
    try {
      const params = {text, mode: S.newMode, approval_mode: S.newPolicy};
      if (S.tid) params.talk = S.tid;
      if (docs.length) params.docs = docs;
      if (S.newPreset) params.run_preset = S.newPreset;
      const lc = newChoice();
      if (lc.model) params.llm = lc;
      const r = await rpc('session/start', params);
      S.pending = null;
      if (r && r.goal) {
        S.temp = null;
        await loadGoals();
        await go({g: r.goal});
        return;
      }
      S.temp = [...temp, {role: 'user', text, docs: shown}, {role: 'assistant', text: (r && r.reply) || ''}];
      if (r && r.talk) {
        S.tid = r.talk;
        await loadGoals();
        await go({t: r.talk});
      }
    } catch (e) {
      S.pending = null;
      if (docs.length) { S.atts = docs.map((d, i) => ({key: ++attSeq, hash: d, name: shown[i].name, kind: shown[i].kind})); renderAtts(); }
      S.temp = [...temp, {role: 'user', text, docs: shown}, {role: 'assistant', text: e.message, err: true}];
    }
    S.toEnd = true;
    renderAll();
    return;
  }
  try { await rpc('task/submit', {goal: S.gid, kind: 'text', input: docs.length ? {text, docs} : {text}}); }
  catch (e) { toast(e.message); }
  S.pending = null; S.toEnd = true;
  await refresh(false);
}
function blankLayout() {
  const inp = document.querySelector('input[data-layout-n]');
  const n = inp ? Number(inp.value.trim()) : 0;
  if (!Number.isInteger(n) || n < 3 || n > 31) {
    toast('Pins per side must be an integer between 3 and 31');
    if (inp) inp.focus();
    return;
  }
  const rows = Array.from({length: n}, () => 'F'.repeat(n));
  return act(() => rpc('task/submit', {goal: S.gid, kind: 'extract_layout', input: {rows}}));
}
async function flipCell(cell) {
  const [r, c] = cell.split(':').map(Number);
  const pl = (await node(S.snap.goal.spec.paramlist)).content;
  const rows = pl.params.find(p => p.id === 'layout').value.map(x => x.split(''));
  rows[r][c] = {F: 'G', G: 'I', I: 'F'}[rows[r][c]];
  await act(() => rpc('task/submit', {goal: S.gid, kind: 'extract_layout', input: {rows: rows.map(x => x.join(''))}}));
  const again = document.querySelector(`[data-cell="${cell}"]`);
  if (again) again.focus({preventScroll: true});
}
async function openSource(spec) {
  const [pid, alt] = spec.split(':');
  const pl = (await node(S.snap.goal.spec.paramlist)).content;
  const r = rowsOf(pl).find(x => x.id === pid), p = r && (r.struck || r);
  const o = p && alt !== undefined ? p.alternatives[+alt] : p;
  const rv = S.snap.extraction && S.snap.extraction.review;
  const src = o && o.page ? o : rv && rv.read[pid];
  if (src && src.page) return showSource(src, pid);
}
function srcTurn(step) {
  const s = S.src, m = S.snap.docs.find(x => x.hash === s.hash);
  const n = Math.min(m.pages, Math.max(1, s.page + step));
  S.src = {hash: s.hash, page: n, box: null, quote: ''};
  S.panelKey = null;
  return renderPanel(true);
}
function openPanel(p, extra = {}) {
  if (!S.snap) return;
  if (extra.c) S.mcase = extra.c;
  return go({g: S.gid, p, c: extra.c || S.mcase});
}
function setTheme(t) {
  const el = document.documentElement;
  if (t === 'system') delete el.dataset.theme; else el.dataset.theme = t;
  try { if (t === 'system') localStorage.removeItem('p1-theme'); else localStorage.setItem('p1-theme', t); } catch (_) {}
}
function setSide(off) {
  S.sideOff = off;
  $('#app').classList.toggle('side-off', off);
  try { localStorage.setItem('p1-side', off ? 'off' : 'on'); } catch (_) {}
}
const narrow = () => matchMedia('(max-width: 768px)').matches;

document.addEventListener('keydown', ev => {
  if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.dataset && ev.target.dataset.cell) {
    ev.preventDefault();
    flipCell(ev.target.dataset.cell);
  }
});
$('#workDlg').addEventListener('cancel', ev => {
  ev.preventDefault();
  const w = S.workDlg;
  ev.target.close();
  S.workDlg = null;
  if (w) act(() => rpc('proposal/respond', {proposal: w.id, action: 'drop'}));
});
document.addEventListener('click', async ev => {
  const t = ev.target.closest('button, a, [data-panel], [data-region], [data-cell], [data-row], .code .ln');
  if (!t) { if (!ev.target.closest('#pop')) closePop(); return; }
  if (!t.closest('#pop') && t !== popFor) closePop();
  const d = t.dataset;
  S.rv.focus = null;
  const keepFocus = fk => { if (ev.detail === 0) S.rv.focus = fk; };

  if (t.classList.contains('ln')) {
    const f = fieldAtLine(+t.dataset.n);
    if (f) selectField(f === S.mfield ? null : f);
    return;
  }
  if (d.talk) { ev.preventDefault(); if (narrow()) $('#app').classList.remove('nav-open'); if (d.talk !== S.tid) await go({t: d.talk}); return; }
  if (d.policy) { closePop(); await setPolicy(d.policy); if (ev.detail === 0) $('#msg').focus({preventScroll: true}); return; }
  if (d.budget) {
    closePop();
    if (S.snap && !ro() && +d.budget * 3600 !== curBudget()) await act(() => rpc('task/submit', {goal: S.gid, kind: 'budget', input: {hours: +d.budget}}));
    if (ev.detail === 0) $('#msg').focus({preventScroll: true});
    return;
  }
  if (d.goal) { ev.preventDefault(); if (narrow()) $('#app').classList.remove('nav-open'); if (d.goal !== S.gid) await go({g: d.goal}); return; }
  if (d.panel) {
    ev.preventDefault(); ev.stopPropagation();
    if (d.ev) { S.evSel = +d.ev; S.evDetail = null; try { S.evidence = await api(`api/evidence?goal=${S.gid}`); } catch (_) {} }
    if (d.panel === 'evidence' && !d.ev && S.panel !== 'evidence') S.evSel = null;
    if (d.report !== undefined) S.reportHash = d.report || null;
    if (d.solver) S.msolver = d.solver;
    if (d.panel !== S.panel || d.case && d.case !== S.mcase) await openPanel(d.panel, {c: d.case});
    else { S.panelKey = null; await renderPanel(true); }
    return;
  }
  if (d.attRm) { S.atts = S.atts.filter(a => String(a.key) !== d.attRm); renderAtts(); return; }
  if (d.row) { if (S.panel === 'params' && splitOn() && !ev.target.closest('input, select, textarea, label, svg')) await openSource(d.row); return; }
  if (d.src) { await openSource(d.src); return; }
  if (d.srcV) { const v = checkValue(d.srcV); if (v) await showSource(v, d.srcV.split('|')[1]); return; }
  if (d.srcZoom) { S.srcZoom = Math.min(3, Math.max(1, S.srcZoom + 0.5 * +d.srcZoom)); S.panelKey = null; await renderPanel(true); return; }
  if (d.rv) { t.disabled = true; keepFocus(`${{extract_strike: 'undo', extract_unstrike: 'del'}[d.rv] || 'val'}:${d.p}`); if (!await rvDo(d.rv, {param: d.p})) t.disabled = false; return; }
  if (d.rvScope) { S.rv.scope = d.rvScope; repaintList(); return; }
  if (d.rvOpen) { S.rv.open[d.rvOpen] = !S.rv.open[d.rvOpen]; repaintList(); return; }
  if (d.mixReset) { delete S.rv.draft[d.mixReset]; S.rv.open[d.mixReset] = false; repaintList(); return; }
  if (d.mixSave) { const box = $(`[data-mix="${CSS.escape(d.mixSave)}"]`); if (await rvDo('extract_mixture', {param: d.mixSave, text: box.value})) delete S.rv.draft[d.mixSave]; return; }
  if (d.adopt) { const [check, param, alt] = d.adopt.split('|'); t.disabled = true; keepFocus(`val:${param}`); if (!await rvDo('extract_adopt', {check, param, alt: alt === '' ? null : +alt})) t.disabled = false; return; }
  if (d.adoptAll) { t.disabled = true; if (!await rvDo('extract_adopt_all', {check: d.adoptAll})) t.disabled = false; return; }
  if (d.checkRm) { t.disabled = true; if (S.rv.scope === d.checkRm) S.rv.scope = 'all'; if (!await rvDo('extract_check_remove', {check: d.checkRm})) t.disabled = false; return; }
  if (d.chkModel) { setCheckChoice({group: d.chkGroup, model: d.chkModel}); closePop(); repaintList(); return; }
  if (d.chkEffort) { setCheckChoice({effort: d.chkEffort}); $('#pop').innerHTML = checkMenu(); repaintList(); return; }
  if (d.pick) { const [pid, i] = d.pick.split(':'); t.disabled = true; if (!await rvDo('extract_pick', {param: pid, alternative: +i})) t.disabled = false; return; }
  if (d.unpick) { t.disabled = true; if (!await rvDo('extract_unpick', {param: d.unpick})) t.disabled = false; return; }
  if ('blankLayout' in d) { await blankLayout(); return; }
  if (d.cell) { await flipCell(d.cell); return; }
  if (d.ptab) { if (d.ptab === 'evidence') S.evSel = null; await openPanel(d.ptab); return; }
  if (d.mcase) { S.mcase = d.mcase; S.preview = null; await go({g: S.gid, p: S.panel, c: d.mcase}, true); return; }
  if (d.field) { selectField(d.field === S.mfield ? null : d.field); if (S.panel === 'input') { S.panelKey = null; renderPanel(true); } return; }
  if (d.region && S.mctx) {
    const f = Object.keys(S.mctx.fields).find(k => regionOf(k) === d.region && /density|outer_radius|pitch/.test(k));
    if (f) selectField(f);
    return;
  }
  if (d.solver) { S.msolver = d.solver; await renderPanel(true); return; }
  if (d.plot !== undefined) { S.plotOn = d.plot === '1'; await renderPanel(true); return; }
  if (d.evsel !== undefined) { S.evSel = +d.evsel || null; S.evDetail = null; await renderPanel(true); $('#pbody').scrollTop = 0; return; }
  if (d.open) { await openArtifact(d.open); return; }
  if (d.reportOpen) { S.reportHash = d.reportOpen; await renderPanel(true); return; }
  if (d.pvcase) { const i = d.pvcase.lastIndexOf(':'); S.open[d.pvcase.slice(0, i) + ':case'] = d.pvcase.slice(i + 1); renderThread(); if (S.panel === 'model') { S.panelKey = null; renderPanel(true); } return; }
  if (d.approve || d.reject) {
    const ids = (d.approve || d.reject).split(','), dec = d.approve ? 'approve' : 'reject';
    t.disabled = true;
    await act(async () => { for (const id of ids) await rpc('approval/respond', {approval: id, decision: dec}); });
    if (ev.detail === 0) $('#msg').focus({preventScroll: true});
    return;
  }
  if (d.dopt) {
    const i = d.dopt.indexOf(':'), did = d.dopt.slice(0, i), oid = d.dopt.slice(i + 1);
    const inp = $(`[data-dval="${d.dopt}"]`);
    if (inp && inp.value.trim() === '') { inp.focus(); return; }
    t.disabled = true;
    await act(() => rpc('decision/respond', {decision: did, option: oid, value: inp ? +inp.value : null}));
    if (ev.detail === 0) $('#msg').focus({preventScroll: true});
    return;
  }
  if (d.apply) {
    const pv = S.previews[d.apply];
    if (!pv || pv.error) return;
    t.disabled = true;
    const input = d.uid ? {changes: pv.changes, chat: +d.uid} : {changes: pv.changes};
    return act(() => rpc('task/submit', {goal: S.gid, kind: 'change_params', input}));
  }
  if (d.readingApply) {
    const [pid, i] = d.readingApply.split(':');
    t.disabled = true;
    return act(() => rpc('task/submit', {goal: S.gid, kind: 'change_reading', input: {param: pid, alternative: +i}}));
  }
  if (d.reading) {
    if (!d.sure) { S.pendingReading[d.reading] = true; S.panelKey = null; await renderPanel(true); return; }
    const [pid, i] = d.reading.split(':');
    delete S.pendingReading[d.reading];
    return act(() => rpc('task/submit', {goal: S.gid, kind: 'change_reading', input: {param: pid, alternative: +i}}));
  }
  if (d.readingCancel) { delete S.pendingReading[d.readingCancel]; S.panelKey = null; await renderPanel(true); return; }
  if (d.preset) return setPreset(d.preset, true);
  if (d.mode) {
    if (d.mode === curMode() || S.snap) return;
    S.newMode = d.mode; renderTop(); renderDock(); return;
  }
  if (d.prop) {
    const [id, a] = d.prop.split(':');
    t.disabled = true;
    await act(() => rpc('proposal/respond', {proposal: id, action: a}));
    if (ev.detail === 0) $('#msg').focus({preventScroll: true});
    return;
  }
  if (d.workOpen) { openWorkDialog(d.workOpen); return; }
  if (d.workDrop) { t.disabled = true; return act(() => rpc('proposal/respond', {proposal: d.workDrop, action: 'drop'})); }
  if (d.workPol) { S.workDlg.policy = d.workPol; renderWorkDlg(); $(`#workDlg [data-work-pol="${d.workPol}"]`).focus(); return; }
  if (d.workCancel || d.workGo) {
    const w = S.workDlg, dlg = $('#workDlg');
    dlg.close();
    S.workDlg = null;
    if (d.workCancel) return act(() => rpc('proposal/respond', {proposal: w.id, action: 'drop'}));
    S.toEnd = true;
    const body = {proposal: w.id, action: 'apply', version: w.version, policy: w.policy};
    if (d.workGo === 'edit' && w.edit) body.edit = w.edit;
    return act(() => rpc('proposal/respond', body));
  }
  if (d.dlg) { await dlgAct(d.dlg); return; }
  if (d.ndEn) return ndToggle(d.ndEn);
  if (d.ndStop) return ndStop(d.ndStop);
  if (d.nd === 'close') { $('#nodeDlg').close(); return; }
  if (d.dlgProv) { S.dlg.form.provider = d.dlgProv; S.dlg.sure = false; dropShownKey(); renderDlg(); return; }
  if (d.dlgG) { try { await dlgSync(d.dlgG); } catch (e) { toast(e.message); } return; }
  if (d.llmModel) { closePop(); return setChoice({group: d.llmGroup, model: d.llmModel}); }
  if (d.llmEffort) { await setChoice({effort: d.llmEffort}); if (popFor && popFor.id === 'mpick') $('#pop').innerHTML = pickMenu(); return; }
  if ('rlAdd' in d) return rlAdd();
  if (d.rlOff) return rlStrike(d.rlOff);
  if (d.rlUndo) return rlUndo(d.rlUndo);
  if (d.rlCancel) return rlCancel(d.rlCancel);
  if (d.rlCase) return rlToggleCase(d.rlKey, d.rlCase);
  if (d.rlPick) return rlTogglePick(d.rlPick);
  if (d.rlIns) return rlInsert(d.rlKey, d.rlIns);
  if (d.theme) { setTheme(d.theme); $('#pop').innerHTML = envMenu(); $(`#pop [data-theme="${d.theme}"]`).focus({preventScroll: true}); return; }

  switch (d.act) {
    case 'new': ev.preventDefault(); if (narrow()) $('#app').classList.remove('nav-open'); S.temp = null; S.pending = null; S.newMode = 'chat'; S.newPreset = null;
      if (S.gid || S.tid) await go({}); else { renderAll(); }
      $('#msg').focus(); return;
    case 'nav': $('#app').classList.add('nav-open'); return;
    case 'nav-close': $('#app').classList.remove('nav-open'); return;
    case 'side-off': if (narrow()) $('#app').classList.remove('nav-open'); else setSide(true); return;
    case 'side-on': setSide(false); return;
    case 'search': {
      S.searchOn = !S.searchOn;
      $('#searchBox').hidden = !S.searchOn;
      $('#searchBtn').setAttribute('aria-pressed', S.searchOn);
      if (S.searchOn) $('#searchIn').focus(); else { S.search = ''; $('#searchIn').value = ''; renderSide(); }
      return;
    }
    case 'toggle-arch': S.showArchived = !S.showArchived; renderSide(); return;
    case 'env': openPop(t, envMenu(), 'right-up'); refreshEnvMenu(); return;
    case 'plus': openPop(t, presetMenu(), 'right-up'); if (popFor === t) { estOpen(); renderEst(); } return;
    case 'preset-apply': return setPreset(pinCheck());
    case 'add-file': closePop(); $('#fileIn').click(); return;
    case 'extract-confirm': t.disabled = true; S.toEnd = true; return act(async () => {
      const gid = S.gid;
      if (!await saveSettled()) { t.disabled = false; return; }
      try { await rpc('task/submit', {goal: gid, kind: 'extract_confirm', input: {seen: S.snap.goal.spec.paramlist}}); } catch (e) { t.disabled = false; throw e; }
      if (S.gid === gid) await go({g: gid});
    });
    case 'rv-check': return startCheck();
    case 'rv-check-model': openPop(t, checkMenu(), 'below'); return;
    case 'rv-add': return openAdd();
    case 'rl-save': return rlSave();
    case 'rl-discard': return rlDiscard();
    case 'rv-add-save': return saveAdded();
    case 'rv-add-cancel': S.rv.adding = false; return repaintList();
    case 'extract-retry': t.disabled = true; S.toEnd = true; return act(() => rpc('task/submit', {goal: S.gid, kind: 'extract_retry'}));
    case 'src-prev': case 'src-next': return srcTurn(d.act === 'src-next' ? 1 : -1);
    case 'mpick': openPop(t, pickMenu(), 'below'); return;
    case 'ppick': openPop(t, policyMenu(), 'below'); return;
    case 'llm-settings': closePop(); return openLlmDialog();
    case 'node-settings': closePop(); return openNodeDialog();
    case 'think-toggle':
      S.showThink = !S.showThink;
      try { localStorage.setItem('p1-think', S.showThink ? 'on' : 'off'); } catch (_) {}
      $('#pop').innerHTML = envMenu(); $('#pop [data-act="think-toggle"]').focus({preventScroll: true}); S.threadHtml = null; renderThread(); return;
    case 'more': openPop(t, moreMenu(), 'below'); return;
    case 'start-work': t.disabled = true; S.toEnd = true; return act(() => rpc('task/submit', {goal: S.gid, kind: 'start_work', input: {version: S.snap.goal.version}}));
    case 'panel': return S.panel ? go({g: S.gid}) : openPanel(S.lastPanel || 'params');
    case 'panel-close': return go({g: S.gid});
    case 'pause': return act(() => rpc('run/pause', {goal: S.gid}));
    case 'continue': return act(() => rpc('run/continue', {goal: S.gid}));
    case 'stop': t.disabled = true; return act(() => rpc('session/interrupt', {goal: S.gid}));
    case 'archive': case 'unarchive': closePop();
      if (S.tid) return act(async () => {
        await api('api/talks/archive', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: S.tid, archived: d.act === 'archive'})});
        await loadGoals(); S.sideKey = null; renderAll();
      });
      return act(async () => { await rpc('task/submit', {goal: S.gid, kind: 'archive', input: {archived: d.act === 'archive'}}); await loadGoals(); S.sideKey = null; });
    case 'report': t.disabled = true; return act(async () => { await rpc('task/submit', {goal: S.gid, kind: 'report'}); S.reportHash = null; S.panelKey = null; });
    case 'preview': {
      const v = parseFloat($('#editVal').value);
      if (isNaN(v)) { $('#editVal').focus(); return; }
      try { S.preview = Object.assign(await api('api/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({goal: S.gid, changes: {[d.pid]: v}})}), {changes: {[d.pid]: v}}); }
      catch (e) { toast(e.message); }
      S.panelKey = null; await renderPanel(true); $('#pbody').scrollTop = 0; return;
    }
    case 'unpreview': S.preview = null; S.panelKey = null; await renderPanel(true); return;
    case 'apply-form': { const pv = S.preview; S.preview = null; S.panelKey = null; return act(() => rpc('task/submit', {goal: S.gid, kind: 'change_params', input: {changes: pv.changes}})); }
  }
});
document.addEventListener('toggle', ev => {
  const k = ev.target.dataset && ev.target.dataset.k;
  if (k) S.open[k] = ev.target.open;
  if (k && k.startsWith('ru:') && ev.target.open && !ro()) rlTry(k.slice(3), true);
  if (ev.target.dataset && ev.target.dataset.think && ev.target.open) loadThink(ev.target);
}, true);
document.addEventListener('input', ev => {
  const t = ev.target;
  if (t.id === 'msg') { growInput(); renderSend(); }
  else if (t.id === 'searchIn') { S.search = t.value; renderSide(); }
  else if (t.dataset.dval) S.dvals[t.dataset.dval] = t.value;
  else if (t.dataset.add) S.rv.add[t.dataset.add] = t.value;
  else if (t.dataset.mix) S.rv.draft[t.dataset.mix] = t.value;
  else if (t.dataset.rlF) rlInput(t);
  else if ('rlQ' in t.dataset) rlFilter(t.value);
  else if (t.dataset.pin) estPin();
});
$('#pop').addEventListener('mouseover', ev => { const c = ev.target.closest('[data-preset]'); if (c) estHover(c.dataset.preset); });
$('#pop').addEventListener('mouseout', ev => {
  const c = ev.target.closest('[data-preset]');
  if (c && !(ev.relatedTarget && ev.relatedTarget.closest && ev.relatedTarget.closest('[data-preset]'))) estHover(null);
});
$('#pop').addEventListener('focusin', ev => { const c = ev.target.closest('[data-preset]'); estHover(c ? c.dataset.preset : null); });
let holdTimer;
function holdRepaint(select) {
  S.rv.held = select ? 'select' : true;
  clearTimeout(holdTimer);
  holdTimer = setTimeout(releaseHold, select ? 15000 : 1500);
}
function releaseHold() {
  clearTimeout(holdTimer);
  S.rv.held = false;
  if (S.rv.stale) { S.rv.stale = false; setTimeout(repaintList, 30); }
}
document.addEventListener('pointerdown', ev => {
  S.rv.gesture++;
  if (!ev.target.closest('#panel')) return;
  holdRepaint(ev.target.tagName === 'SELECT');
}, true);
document.addEventListener('pointerup', () => { if (S.rv.held !== 'select') releaseHold(); }, true);
document.addEventListener('pointercancel', releaseHold, true);
document.addEventListener('keydown', ev => {
  S.rv.gesture++;
  if (ev.target.tagName === 'SELECT' && ev.target.closest('#panel') && ((ev.altKey && ev.key === 'ArrowDown') || ev.key === ' ' || ev.key === 'Enter')) holdRepaint(true);
}, true);
document.addEventListener('focusout', ev => {
  const t = ev.target;
  if (t.tagName === 'SELECT' && S.rv.held === 'select') releaseHold();
  if (t.dataset && t.dataset.rlF === 'expr') rlCaret(t);
  if (t.dataset && t.dataset.val && t.tagName === 'INPUT' && !S.rv.repainting) saveValue(t);
});
document.addEventListener('change', ev => {
  if (S.rv.repainting) return;
  const s = ev.target;
  if (s.tagName === 'SELECT' && S.rv.held === 'select') releaseHold();
  if (s.id === 'fileIn') { addFiles(s.files); s.value = ''; return; }
  if (s.dataset.tick) { tickRow(s.dataset.tick, s.checked); return; }
  if (s.id === 'rvAll') { tickAll(s.checked); return; }
  if (s.id === 'rvOnly') { S.rv.only = s.checked; repaintList(); const list = $('#rvL') || $('#pbody'); if (list) list.scrollTop = 0; return; }
  if (s.dataset.val) { saveValue(s); return; }
  if (s.id === 'srcDoc') { S.src = {hash: s.value, page: 1, box: null, quote: ''}; S.panelKey = null; renderPanel(true); return; }
  if (s.id === 'srcPage') {
    const m = S.snap.docs.find(x => x.hash === S.src.hash);
    S.src = {hash: S.src.hash, page: Math.min(m.pages, Math.max(1, parseInt(s.value, 10) || 1)), box: null, quote: ''};
    S.panelKey = null; renderPanel(true); return;
  }
  if (s.matches('select[data-alt]') && s.value) {
    const key = s.selectedOptions[0].dataset.k;
    act(() => rpc('task/submit', {goal: S.gid, kind: 'change_binding', input: {solver: s.dataset.alt, key, choice: s.value}}));
  }
});
$('#composer').addEventListener('submit', ev => { ev.preventDefault(); send(); });
$('#msg').addEventListener('paste', ev => {
  const files = [...(ev.clipboardData ? ev.clipboardData.files : [])];
  if (files.length) { ev.preventDefault(); addFiles(files); }
});
$('#composer').addEventListener('dragover', ev => { if ([...ev.dataTransfer.types].includes('Files')) { ev.preventDefault(); $('#composer').classList.add('drop'); } });
document.addEventListener('dragover', ev => { if ([...ev.dataTransfer.types].includes('Files')) ev.preventDefault(); });
document.addEventListener('drop', ev => {
  if (!ev.dataTransfer.files.length) return;
  ev.preventDefault();
  if (!$('#composer').contains(ev.target)) addFiles(ev.dataTransfer.files);
});
$('#composer').addEventListener('dragleave', ev => { if (!$('#composer').contains(ev.relatedTarget)) $('#composer').classList.remove('drop'); });
$('#composer').addEventListener('drop', ev => {
  $('#composer').classList.remove('drop');
  if (ev.dataTransfer.files.length) { ev.preventDefault(); addFiles(ev.dataTransfer.files); }
});
document.addEventListener('keydown', ev => {
  const ds = ev.target.dataset || {};
  if (ds.val && ev.target.tagName === 'INPUT') {
    if (ev.key === 'Enter') { ev.preventDefault(); ev.target.blur(); return; }
    if (ev.key === 'Escape') { ev.target.value = ds.was; ev.target.blur(); return; }
  }
  if ('rlQ' in ds && ev.key === 'Escape') { ev.stopPropagation(); rlClosePick(); return; }
  if (ds.add && ev.key === 'Enter') { ev.preventDefault(); saveAdded(); return; }
  if (ds.pin && ev.key === 'Enter' && !ev.isComposing) { ev.preventDefault(); const key = pinCheck(); if (key) setPreset(key); return; }
  if (ds.add && ev.key === 'Escape') { S.rv.adding = false; repaintList(); return; }
  if (ev.key === 'Escape' && ev.target.closest && ev.target.closest('#panel') && ev.target.matches('input:not([type="checkbox"]), textarea, select')) { ev.target.blur(); return; }
  if (ev.target.id === 'msg' && ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); send(); return; }
  if ((ev.key === 'ArrowDown' || ev.key === 'ArrowUp') && ev.target.matches('#pop [role^="menuitem"]')) {
    const items = [...$('#pop').querySelectorAll('[role^="menuitem"]:not(:disabled)')];
    ev.preventDefault();
    items[(items.indexOf(ev.target) + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length].focus();
    return;
  }
  if (ev.key === 'Escape') {
    if (!$('#pop').hidden) { closePop(); return; }
    if ($('#app').classList.contains('nav-open')) { $('#app').classList.remove('nav-open'); return; }
    if (S.searchOn && ev.target.id === 'searchIn') { $('[data-act="search"]').click(); return; }
    if (S.panel) go({g: S.gid});
  }
});
window.addEventListener('popstate', applyRoute);
window.addEventListener('hashchange', applyRoute);
window.addEventListener('resize', closePop);
let splitWas = null;
new ResizeObserver(() => {
  if (S.panel !== 'params' || !S.snap) return;
  const now = splitOn();
  if (now !== splitWas) { splitWas = now; S.panelKey = null; renderPanel(true); }
}).observe($('#panel'));

(async function init() {
  $('#searchBtn').innerHTML = ic('search');
  $('#sideOffBtn').innerHTML = ic('side');
  $('#newBtn').innerHTML = `${ic('pen')}<span>New chat</span>`;
  try { const p = localStorage.getItem('p1-policy'); if (p === 'ask' || p === 'goal') S.newPolicy = p; } catch (_) {}
  $('#pclose').innerHTML = ic('close');
  $('#pside').innerHTML = ic('side');
  try { if (localStorage.getItem('p1-side') === 'off') setSide(true); } catch (_) {}
  try { S.newLlm = JSON.parse(localStorage.getItem('p1-llm') || 'null'); } catch (_) {}
  try { S.showThink = localStorage.getItem('p1-think') !== 'off'; } catch (_) {}
  renderAvatar();
  try { S.health = await api('api/health'); } catch (_) {}
  renderAvatar(); renderPick();
  loadNodes();
  await loadGoals();
  await applyRoute();
  if (!S.gid) connect();
  renderPick();
  setInterval(async () => { try { S.health = await api('api/health'); renderAvatar(); renderPick(); } catch (_) {} loadNodes(); }, 60000);
})();
