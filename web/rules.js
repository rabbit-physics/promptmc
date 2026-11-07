'use strict';
/* Rules panel: built-in rules (read-only) and user rules (staged edits saved atomically) */

const RANKV = {pass: 1, na: 0, info: 0, warn: 2, override: 2, fail: 3, error: 3};
const worstOf = sts => sts.reduce((w, s) => (RANKV[s] ?? 0) > (RANKV[w] ?? -1) ? s : w, sts.length ? 'na' : 'none');
function builtinStatus() {
  const out = {};
  (S.snap.rules.builtin || []).forEach(r => { out[r.id] = r.results || {}; });
  return out;
}
const anyText = v => v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v);
const USER_RES = {true: ['pass', 'Pass'], false: ['fail', 'Fail'], null: ['info', 'N/A']};
function caseStrip(map, fn) {
  return `<div class="rl-cases">${S.snap.cases.filter(c => map && c.case in map).map(c => { const [k, l] = fn(map[c.case]);
    return `<span class="rl-c" title="${h(c.case)} ${h(l)}">${glyph(k, `${c.case} ${l}`)}<span class="num">${h(c.case)}</span></span>`; }).join('')}</div>`;
}

/* ---------- Draft Edit State ---------- */
const rlNew = () => ({edits: {}, adds: [], off: {}, on: {}, tries: {}, seq: 0, pick: null, q: '', fields: null, caret: {}, seen: null, saving: false, ver: null});
S.rls = {};
const rlOf = () => S.rls[S.gid] || (S.rls[S.gid] = rlNew());
const rlSaved = key => ((S.snap.rules || {}).user || []).find(r => r.id === key);
const rlCaseIds = () => S.snap.cases.map(c => c.case);
function rlCur(key) {
  const rl = rlOf();
  return rl.adds.find(a => a.key === key) || rl.edits[key] || rlSaved(key) || null;
}
const rlAllOn = cases => !cases || cases.length === rlCaseIds().length;
function rlSame(a, b) {
  const cs = x => JSON.stringify(rlAllOn(x.cases) ? null : [...x.cases].sort());
  return a.label.trim() === b.label.trim() && a.expr.trim() === b.expr.trim() && cs(a) === cs(b);
}
function rlDirty() {
  const rl = rlOf();
  return Object.keys(rl.edits).length + rl.adds.length + Object.keys(rl.off).length + Object.keys(rl.on).length;
}
function rlSet(key, patch) {
  const rl = rlOf(), add = rl.adds.find(a => a.key === key);
  if (!rl.seen) rl.seen = S.snap.rules.seen;
  if (add) { Object.assign(add, patch); return; }
  const r = rlSaved(key);
  if (!r) return;
  const next = Object.assign({label: r.label, expr: r.expr, cases: r.cases || null}, rl.edits[key], patch);
  if (rlSame(next, r)) delete rl.edits[key]; else rl.edits[key] = next;
}
function rlRes(key) {
  const rl = rlOf(), cur = rlCur(key), t = rl.tries[key];
  if (t && cur && t.sent === JSON.stringify([cur.expr, cur.cases])) return t;
  const r = rlSaved(key);
  return r && !rl.edits[key] ? {results: r.results || {}, values: {}, error: null} : {results: {}, values: {}, error: null, waiting: !!(cur && cur.expr.trim())};
}
function rlProblem(key) {
  const cur = rlCur(key), res = rlRes(key);
  if (cur.cases && !cur.cases.length) return 'Select at least one case';
  return cur.expr.trim() ? res.error || '' : '';
}
function rlReady() {
  const rl = rlOf();
  const live = [...Object.keys(rl.edits).filter(k => !rl.off[k]), ...rl.adds.map(a => a.key)];
  return !rl.saving && rlDirty() > 0 && live.every(k => rlCur(k).expr.trim() && !rlProblem(k) && !rlRes(k).waiting);
}

/* ---------- Rendering ---------- */
function rlVerdict(res) {
  if (res.error) return 'none';
  const vals = Object.values(res.results || {});
  return vals.includes(false) ? 'fail' : vals.includes(true) ? 'pass' : vals.length ? 'na' : 'none';
}
function rlChips(key, cur, res) {
  return rlCaseIds().map(c => {
    const on = !cur.cases || cur.cases.includes(c), v = res.error ? undefined : (res.results || {})[c];
    const [k, l] = !on ? [null, 'Excluded'] : v === undefined ? [null, ''] : USER_RES[String(v)];
    return `<button class="rl-chip num" type="button" data-rl-key="${h(key)}" data-rl-case="${h(c)}" data-fk="rlc:${h(key)}:${h(c)}" aria-pressed="${on}" title="${h(`${c} ${l}`.trim())}" aria-label="${h(`${c} ${l}`.trim())}">${k ? glyph(k, `${c} ${l}`) : ''}${h(c)}</button>`;
  }).join('');
}
function rlVals(res) {
  return Object.entries(res.values || {}).slice(0, 4).map(([p, m]) => {
    const xs = Object.values(m), same = xs.every(x => x === xs[0]);
    return `<div><span class="mono">${h(p)}</span> <span class="num">${same ? h(showVal(xs[0])) : Object.entries(m).map(([c, v]) => `${h(c)} ${h(showVal(v))}`).join(', ')}</span></div>`;
  }).join('');
}
function rlPickHtml(key) {
  const rl = rlOf();
  if (rl.pick !== key) return '';
  return `<div class="rl-pick"><input class="rv-in" type="text" data-rl-q data-fk="rlq" value="${h(rl.q)}" placeholder="Search fields" aria-label="Search fields" autocomplete="off">
    <ul class="rl-plist" data-rl-list="${h(key)}">${rlPickRows(key)}</ul></div>`;
}
function rlPickRows(key) {
  const rl = rlOf();
  if (!rl.fields) return '<li class="rl-none">Loading fields</li>';
  const words = rl.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows = Object.entries(rl.fields.fields).map(([p, m]) => {
    const vals = Object.values(m), same = vals.every(x => x === vals[0]);
    return {p, label: fieldLabel(p) || p, val: same ? showVal(vals[0]) : `${Object.keys(m).pop()} ${showVal(vals[vals.length - 1])}`};
  }).filter(r => words.every(w => `${r.label} ${r.p}`.toLowerCase().includes(w)));
  if (!rows.length) return '<li class="rl-none">No matching fields</li>';
  return rows.map(r => `<li><button class="rl-pi" type="button" data-rl-key="${h(key)}" data-rl-ins="${h(r.p)}"><span>${h(r.label)}</span><span class="mono">${h(r.p)}</span><span class="num">${h(r.val)}</span></button></li>`).join('');
}
function rlEditor(key, cur, res, r) {
  const k = h(key), rl = rlOf(), err = rlProblem(key);
  const f = (name, label, html) => `<label class="rl-f"><span>${label}</span>${html}</label>`;
  const quote = r && r.text && !r.edited ? `<blockquote>${h(r.text)}</blockquote>` : '';
  return `${quote}
    ${f('label', 'Name', `<input class="rv-in" type="text" data-rl-f="label" data-rl-k="${k}" data-fk="rl:${k}:label" value="${h(cur.label)}" placeholder="${h(cur.expr) || 'Rule description'}" autocomplete="off">`)}
    ${f('expr', 'Expression', `<textarea class="rv-area" rows="2" data-rl-f="expr" data-rl-k="${k}" data-fk="rl:${k}:expr" spellcheck="false" placeholder="regions.moderator.density_g_cm3 >= 0.72">${h(cur.expr)}</textarea>`)}
    <div class="rl-tools"><button class="btn sm" type="button" data-rl-pick="${k}" aria-expanded="${rl.pick === key}" data-fk="rlp:${k}">Insert field</button>
      <span class="sl-r">Supports + - * / **, comparisons, and/or/not, abs/min/max; prefix S0.field to cross-reference cases</span></div>
    ${rlPickHtml(key)}
    <div class="rl-chips" role="group" aria-label="Applicable cases" data-rl-chips>${rlChips(key, cur, res)}</div>
    <div class="rl-vals" data-rl-vals>${rlVals(res)}</div>
    <p class="rl-err" data-rl-err${err ? '' : ' hidden'}>${h(err)}</p>`;
}
function rlRow(r) {
  const rl = rlOf(), key = r.id, k = h(key);
  if (rl.off[key]) {
    return `<li class="rl-gone" data-rl="${k}"><div class="rl-gone-t"><s class="pl-l">${h(r.label)}</s><s class="rl-id mono">${k}</s>
      <button class="ib xs" type="button" data-rl-undo="${k}" data-fk="rlu:${k}" aria-label="Restore ${h(r.label)}" title="Restore">${ic('undo')}</button></div><s class="mono">${h(r.expr)}</s></li>`;
  }
  const cur = rlCur(key), res = rlRes(key), dk = `ru:${key}`;
  return `<li data-rl="${k}"><details data-k="${dk}"${S.open[dk] ? ' open' : ''}><summary><span data-rl-gl>${vglyph(rlVerdict(res))}</span><span class="pl-l" data-rl-name>${h(cur.label || cur.expr)}</span>
      <i class="rl-mark" data-rl-dot title="Unsaved"${rl.edits[key] ? '' : ' hidden'}></i><span class="rl-id mono">${k}</span></summary>
    <div class="pl-d rl-ed">${rlEditor(key, cur, res, r)}
      <div class="rl-acts"><button class="ib xs" type="button" data-rl-undo="${k}" data-rl-dirty data-fk="rlu:${k}" aria-label="Revert ${h(r.label)}" title="Revert"${rl.edits[key] ? '' : ' hidden'}>${ic('undo')}</button>
        <button class="ib xs" type="button" data-rl-off="${k}" data-fk="rlx:${k}" aria-label="Remove ${h(r.label)}" title="Remove">${ic('close')}</button></div></div></details></li>`;
}
function rlReadOnly(r) {
  const res = r.results || {}, dk = `ru:${r.id}`;
  return `<li><details data-k="${dk}"${S.open[dk] ? ' open' : ''}><summary>${vglyph(rlVerdict({results: res}))}<span class="pl-l">${h(r.label)}</span><span class="rl-id mono">${h(r.id)}</span></summary>
    <div class="pl-d">${r.text && !r.edited ? `<blockquote>${h(r.text)}</blockquote>` : ''}<div class="mono sm">${h(r.expr)}</div>
      ${(r.cases || []).length ? `<div><span class="mut">Cases</span> <span class="num">${h(r.cases.join(', '))}</span></div>` : ''}
      ${caseStrip(res, v => USER_RES[String(v)] || USER_RES.null)}</div></details></li>`;
}
function rlRemovedRow(r) {
  const rl = rlOf(), k = h(r.id), back = rl.on[r.id];
  return `<li class="rl-gone" data-rl="${k}"><div class="rl-gone-t">${back ? `<span class="pl-l">${h(r.label)}</span>` : `<s class="pl-l">${h(r.label)}</s>`}${back ? '<i class="rl-mark" title="Unsaved"></i>' : ''}<span class="rl-id mono">${back ? k : `<s>${k}</s>`}</span>
    ${ro() ? '' : `<button class="ib xs" type="button" data-rl-undo="${k}" data-fk="rlu:${k}" aria-label="${back ? 'Cancel restore' : 'Restore'} ${h(r.label)}" title="${back ? 'Cancel restore' : 'Restore'}"${back ? ' aria-pressed="true"' : ''}>${ic(back ? 'close' : 'undo')}</button>`}</div>${back ? `<span class="mono">${h(r.expr)}</span>` : `<s class="mono">${h(r.expr)}</s>`}</li>`;
}
function rlAddRow(a) {
  const res = rlRes(a.key), k = h(a.key);
  return `<li class="rl-new" data-rl="${k}"><div class="pl-d rl-ed">${rlEditor(a.key, a, res, null)}
    <div class="rl-acts"><button class="ib xs" type="button" data-rl-cancel="${k}" data-fk="rlx:${k}" aria-label="Cancel rule" title="Cancel">${ic('close')}</button></div></div></li>`;
}
function rlBar() {
  return `<button class="btn sm pri" type="button" data-act="rl-save" id="rlSave"${rlReady() ? '' : ' disabled'}${rlDirty() ? '' : ' hidden'}>Save</button>
    <button class="btn sm ghost" type="button" data-act="rl-discard" id="rlDrop"${rlDirty() ? '' : ' hidden'}>Discard</button>`;
}
async function rulesHtml() {
  const rs = S.snap.rules;
  if (!rs) return '';
  const user = rs.user || [], gone = rs.removed || [], editing = !ro();
  if (!editing && !(rs.builtin || []).length && !user.length) return '';
  const rl = rlOf(), ver = S.snap.goal.version;
  if (rl.ver !== ver) {
    rl.ver = ver; rl.tries = {};
    if (editing) user.filter(r => S.open[`ru:${r.id}`]).forEach(r => rlTry(r.id, true));
    rl.adds.forEach(a => rlTry(a.key, true));
    Object.keys(rl.edits).forEach(k => rlTry(k, true));
  }
  ['edits', 'off'].forEach(p => Object.keys(rl[p]).forEach(k => { if (!rlSaved(k)) delete rl[p][k]; }));
  Object.keys(rl.on).forEach(k => { if (!gone.some(r => r.id === k)) delete rl.on[k]; });
  const bs = builtinStatus();
  const vk = s => VERDICT[s] || VERDICT.none;
  const builtin = (rs.builtin || []).map(r => {
    const per = bs[r.id] || {};
    const k = `rb:${r.id}`;
    return `<li><details data-k="${h(k)}"${S.open[k] ? ' open' : ''}><summary>${vglyph(worstOf(Object.values(per)))}<span class="pl-l">${h(r.name)}</span><span class="rl-id mono">${h(r.id)}</span></summary>
      <div class="pl-d">${r.applies_when ? `<div><span class="mut">Scope</span> ${h(anyText(r.applies_when))}</div>` : ''}
        ${r.check ? `<div><span class="mut">Check</span> ${h(anyText(r.check))}</div>` : ''}${caseStrip(per, vk)}</div></details></li>`;
  }).join('');
  const rows = editing ? user.map(rlRow).join('') + gone.map(rlRemovedRow).join('') + rl.adds.map(rlAddRow).join('') : user.map(rlReadOnly).join('');
  const list = rows ? `<ul class="plist">${rows}</ul>` : '';
  const more = editing ? `<div class="rl-more"><button class="btn sm ghost rv-addb" type="button" data-rl-add data-fk="rl-add">${ic('plus')}<span>Add rule</span></button><span class="grow"></span>${rlBar()}</div>` : '';
  return `<section class="psec"><h3>Rules</h3>
    ${list || more ? `<h4 class="rl-h">User</h4>${list}${more}` : ''}
    ${builtin ? `<h4 class="rl-h">Built-in</h4><ul class="plist">${builtin}</ul>` : ''}</section>`;
}

/* ---------- Partial Updates ---------- */
function rlPaintBar() {
  const s = $('#rlSave'), d = $('#rlDrop');
  if (!s || !d) return;
  const n = rlDirty();
  s.hidden = d.hidden = !n;
  s.disabled = !rlReady();
  S.rvHtml = null;
}
function rlPaint(key) {
  const li = document.querySelector(`[data-rl="${CSS.escape(key)}"]`), cur = rlCur(key);
  if (li && cur) {
    const q = s => li.querySelector(s), res = rlRes(key), err = rlProblem(key), rl = rlOf();
    const a = document.activeElement, fk = a && li.contains(a) && a.classList.contains('rl-chip') ? a.dataset.fk : null;
    if (q('[data-rl-chips]')) q('[data-rl-chips]').innerHTML = rlChips(key, cur, res);
    if (fk) { const b = li.querySelector(`[data-fk="${CSS.escape(fk)}"]`); if (b) b.focus({preventScroll: true}); }
    if (q('[data-rl-vals]')) q('[data-rl-vals]').innerHTML = rlVals(res);
    const e = q('[data-rl-err]');
    if (e) { e.textContent = err; e.hidden = !err; }
    if (q('[data-rl-gl]')) q('[data-rl-gl]').innerHTML = vglyph(rlVerdict(res));
    if (q('[data-rl-name]')) q('[data-rl-name]').textContent = cur.label || cur.expr;
    const on = !!rl.edits[key];
    if (q('[data-rl-dot]')) q('[data-rl-dot]').hidden = !on;
    if (q('[data-rl-dirty]')) q('[data-rl-dirty]').hidden = !on;
  }
  rlPaintBar();
}
const rlTimers = {};
function rlTry(key, now) {
  clearTimeout(rlTimers[key]);
  rlTimers[key] = setTimeout(async () => {
    const gid = S.gid, rl = rlOf(), cur = rlCur(key);
    if (!cur || !S.snap) return;
    if (!cur.expr.trim()) { delete rl.tries[key]; rlPaint(key); return; }
    const sent = JSON.stringify([cur.expr, cur.cases]);
    let t;
    try {
      t = await api('api/rules/try', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({goal: gid, expr: cur.expr, cases: cur.cases})});
    } catch (e) { t = {results: {}, values: {}, error: e.message}; }
    if (S.gid !== gid) return;
    rl.tries[key] = Object.assign(t, {sent});
    rlPaint(key);
  }, now ? 0 : 180);
}

/* ---------- Actions ---------- */
function rlInput(t) {
  const key = t.dataset.rlK;
  rlSet(key, {[t.dataset.rlF]: t.value});
  if (t.dataset.rlF === 'expr') rlTry(key);
  rlPaint(key);
}
function rlCaret(t) { rlOf().caret[t.dataset.rlK] = [t.selectionStart, t.selectionEnd]; }
function rlToggleCase(key, c) {
  const cur = rlCur(key), ids = rlCaseIds();
  const on = cur.cases ? [...cur.cases] : [...ids];
  const next = on.includes(c) ? on.filter(x => x !== c) : ids.filter(x => on.includes(x) || x === c);
  rlSet(key, {cases: next.length === ids.length ? null : next});
  rlPaint(key);
  if (next.length) rlTry(key, true);
}
async function rlAdd() {
  const rl = rlOf(), key = `new${++rl.seq}`;
  if (!rl.seen) rl.seen = S.snap.rules.seen;
  rl.adds.push({key, label: '', expr: '', cases: null});
  await repaintList();
  const i = document.querySelector(`[data-fk="rl:${key}:expr"]`);
  if (i) i.focus();
}
function rlStrike(key) {
  const rl = rlOf();
  if (!rl.seen) rl.seen = S.snap.rules.seen;
  rl.off[key] = true;
  S.rv.focus = `rlu:${key}`;
  return repaintList();
}
function rlUndo(key) {
  const rl = rlOf();
  let back = `rlx:${key}`;
  if (rl.off[key]) delete rl.off[key];
  else if (rl.edits[key]) { delete rl.edits[key]; delete rl.tries[key]; }
  else {
    back = `rlu:${key}`;
    if (rl.on[key]) delete rl.on[key];
    else if ((S.snap.rules.removed || []).some(r => r.id === key)) { if (!rl.seen) rl.seen = S.snap.rules.seen; rl.on[key] = true; }
  }
  S.rv.focus = back;
  return repaintList();
}
function rlCancel(key) {
  const rl = rlOf();
  rl.adds = rl.adds.filter(a => a.key !== key);
  delete rl.tries[key];
  S.rv.focus = 'rl-add';
  return repaintList();
}
async function rlTogglePick(key) {
  const rl = rlOf();
  if (rl.pick === key) return rlClosePick();
  rl.pick = key; rl.q = '';
  S.rv.focus = 'rlq';
  await repaintList();
  try { rl.fields = await api(`api/rules/fields?goal=${encodeURIComponent(S.gid)}`); }
  catch (e) { toast(e.message); return rlClosePick(); }
  const l = document.querySelector('[data-rl-list]');
  if (l) { l.innerHTML = rlPickRows(key); S.rvHtml = null; }
}
function rlClosePick() {
  const rl = rlOf(), key = rl.pick;
  rl.pick = null; rl.q = '';
  if (key) S.rv.focus = `rlp:${key}`;
  return repaintList();
}
function rlFilter(v) {
  const rl = rlOf();
  rl.q = v;
  const l = document.querySelector('[data-rl-list]');
  if (l) { l.innerHTML = rlPickRows(l.dataset.rlList); S.rvHtml = null; }
}
async function rlInsert(key, path) {
  const rl = rlOf(), cur = rlCur(key);
  const [a, b] = rl.caret[key] || [cur.expr.length, cur.expr.length];
  rlSet(key, {expr: cur.expr.slice(0, a) + path + cur.expr.slice(b)});
  rl.pick = null; rl.q = '';
  const pos = a + path.length;
  await repaintList();
  const t = document.querySelector(`[data-fk="rl:${CSS.escape(key)}:expr"]`);
  if (t) { t.focus(); t.setSelectionRange(pos, pos); rl.caret[key] = [pos, pos]; }
  rlTry(key, true);
}
async function rlSave() {
  const rl = rlOf(), gid = S.gid;
  if (!rlReady()) return;
  const changes = [...Object.entries(rl.edits).filter(([k]) => !rl.off[k]).map(([id, e]) => ({op: 'edit', id, label: e.label, expr: e.expr, cases: e.cases})),
    ...Object.keys(rl.off).map(id => ({op: 'remove', id})),
    ...Object.keys(rl.on).map(id => ({op: 'restore', id})),
    ...rl.adds.map(a => ({op: 'add', label: a.label, expr: a.expr, cases: a.cases}))];
  rl.saving = true;
  rlPaintBar();
  try {
    await rpc('task/submit', {goal: gid, kind: 'rules_save', input: {seen: rl.seen || S.snap.rules.seen, changes}});
    S.rls[gid] = rlNew();
  } catch (e) {
    toast(e.message);
    rl.saving = false;
    rl.seen = null;
  }
  await refresh(false);
  S.rv.focus = 'rl-add';
  await repaintList();
}
function rlDiscard() {
  S.rls[S.gid] = rlNew();
  S.rv.focus = 'rl-add';
  return repaintList();
}
