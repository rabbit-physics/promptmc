'use strict';
/* Parameter list item-by-item review: ticks, edits, reverts, strikes, additions, cross-check diffs, split view */

const newRv = () => ({gesture: 0, scope: 'all', only: false, sel: null, adding: false, add: {label: '', value: '', unit: '', page: ''}, want: {}, open: {}, draft: {}, seen: {}, scroll: 0});
S.rv = newRv();

/* Show parameter list and source page side-by-side when workspace panel is wide enough */
const splitOn = () => !narrow() && $('#panel').clientWidth >= 820 && !!(S.snap.docs || []).length && !!S.snap.extraction;

/* Patch panel HTML while preserving focus, active input value, and caret position */
function patchHtml(el, html, memo) {
  if (S[memo] === html && el.childElementCount) return;
  if (S.rv.held) { S.rv.stale = true; return; }
  const a = document.activeElement, fk = a && el.contains(a) ? a.dataset.fk : null;
  const typing = fk && (a.tagName === 'TEXTAREA' || (a.tagName === 'INPUT' && a.type === 'text'));
  const dirty = typing && a.value !== a.defaultValue ? [a.value, a.selectionStart, a.selectionEnd] : null;
  const caret = typing ? [a.selectionStart, a.selectionEnd] : null;
  const top = el.scrollTop;
  S.rv.repainting = true;
  el.innerHTML = html;
  S.rv.repainting = false;
  S[memo] = html;
  el.scrollTop = top;
  const find = k => el.querySelector(`[data-fk="${CSS.escape(k)}"]`);
  const want = S.rv.focus && (find(S.rv.focus) || find(S.rv.focus.replace(/^val:/, 'open:')));
  if (want) { S.rv.focus = null; want.focus({preventScroll: true}); return; }
  if (!fk) return;
  const b = el.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
  if (!b) return;
  b.focus({preventScroll: true});
  if (dirty && a.dataset.g === b.dataset.g) { b.value = dirty[0]; b.setSelectionRange(dirty[1], dirty[2]); }
  else if (caret && b.value === a.value) b.setSelectionRange(caret[0], caret[1]);
}

/* ---------- Split View ---------- */
function scrollToMark(box) {
  const m = box.querySelector('mark, .srcbox');
  if (!m) return;
  const show = () => m.scrollIntoView({block: 'center'});
  const img = box.querySelector('.srcv img');
  if (img && !img.complete) img.addEventListener('load', show, {once: true}); else show();
}
async function renderSplit(key) {
  const el = $('#pbody');
  if (!el.classList.contains('split')) {
    el.classList.add('split');
    el.innerHTML = '<div class="rv-l" id="rvL"></div><div class="rv-r" id="rvR"></div>';
    S.rvHtml = S.srcKey = null;
  }
  const pl = (await node(S.snap.goal.spec.paramlist)).content;
  if (!S.src) {
    const first = rowsOf(pl).find(p => p.page);
    if (first) { S.src = sourceOf(first); S.rv.sel = S.rv.sel || first.id; }
  }
  if (!S.snap) return;
  const html = await viewParams();
  if (S.panelKey !== key) return;
  patchHtml($('#rvL'), html, 'rvHtml');
  afterReview();
  const k = JSON.stringify([S.src, S.srcZoom, S.snap.docs.length]);
  if (k === S.srcKey) return;
  const src = await viewSource();
  if (S.panelKey !== key) return;
  S.srcKey = k;
  const R = $('#rvR');
  R.innerHTML = src;
  scrollToMark(R);
}
const sourceOf = o => ({hash: o.doc || S.snap.docs[0].hash, page: o.page || 1, box: o.bbox || null, quote: o.quote || ''});
async function showSource(o, pid) {
  S.src = sourceOf(o);
  if (pid) S.rv.sel = pid;
  S.panelKey = null;
  if (S.panel === 'params' && splitOn()) return renderPanel(true);
  if (S.panel === 'params') S.rv.scroll = $('#pbody').scrollTop;
  if (S.panel === 'source') { await renderPanel(true); $('#pbody').scrollTop = 0; } else await openPanel('source');
  scrollToMark($('#pbody'));
}
function afterReview() {
  const all = $('#rvAll'), ex = S.snap.extraction, rv = ex && ex.review;
  if (all && rv) {
    all.indeterminate = !all.checked && rv.need.some(i => tickedNow(i, rv));
  }
}

/* ---------- Checkboxes ---------- */
const rowsOf = pl => [...pl.params, ...(pl.added || [])];
const tickedNow = (id, rv) => id in S.rv.want ? S.rv.want[id] : rv.ticks.includes(id);
function repaintList() { S.panelKey = null; return renderPanel(true); }
async function rvDo(kind, input, onFail, goal = S.gid) {
  let ok = true;
  try { await rpc('task/submit', {goal, kind, input}); }
  catch (e) { ok = false; toast(e.message); if (onFail) onFail(); }
  await refresh(false);
  return ok;
}
/* Ensure in-flight input saves complete cleanly before confirming a tick */
async function saveSettled() {
  const rv = S.rv, gesture = rv.gesture, ok = await rv.saving;
  if (S.rv !== rv) return false;
  if (ok !== false && rv.failed !== gesture) return true;
  rv.failed = null;
  rv.want = {};
  S.rvHtml = null;
  repaintList();
  return false;
}
async function tickRow(id, on) {
  const rv = S.snap.extraction.review, own = S.rv, gid = S.gid, settled = saveSettled();
  own.want[id] = on;
  if (on && own.sel === id && S.panel === 'params' && splitOn()) {
    const pl = (await node(S.snap.goal.spec.paramlist)).content, rows = rowsOf(pl);
    if (S.rv !== own) return;
    const next = [...rows.slice(rows.findIndex(p => p.id === id) + 1), ...rows].find(p => p.id !== id && p.page && !p.struck && !tickedNow(p.id, rv));
    if (next) { S.src = sourceOf(next); S.rv.sel = next.id; }
  }
  repaintList();
  if (!await settled) return;
  await rvDo('extract_tick', {param: id, on, seen: (S.snap.extraction.review || rv).prints[id]}, null, gid);
  delete own.want[id];
  if (S.rv === own) repaintList();
}
async function tickAll(on) {
  const rv = S.snap.extraction.review, own = S.rv, gid = S.gid, settled = saveSettled();
  (on ? rv.tickable : rv.need).forEach(i => { own.want[i] = on; });
  repaintList();
  if (!await settled) return;
  await rvDo('extract_tick_all', {on, seen: S.snap.goal.spec.paramlist}, null, gid);
  own.want = {};
  if (S.rv === own) repaintList();
}

/* ---------- Review Row ---------- */
function valueText(p) {
  if (p.value == null) return '';
  if (p.kind === 'reference') return `${p.value}${p.sigma != null ? ` ± ${p.sigma}` : ''}`;
  return typeof p.value === 'number' ? String(+p.value.toPrecision(10)) : String(p.value);
}
function rvValue(p, ex, edit) {
  const o = p.struck || p, rv = ex.review || {};
  const ro_ = `<span class="num">${h(slotValue(o))}</span> <span class="u">${h(unit(o.unit))}</span>`;
  if (p.struck) return `<s>${ro_}</s>`;
  if (!edit) return ro_;
  const label = h(p.label), id = h(p.id), g = h(S.snap.goal.id);
  if (p.nature === 'conflict') {
    return `<span class="sl-alts">${p.alternatives.map((a, i) => `<span class="sl-alt"><button class="btn sm" type="button" data-pick="${id}:${i}"><span class="num">${h(typeof a.value === 'object' ? 'Mixture' : typeof a.value === 'number' ? `${+a.value.toPrecision(7)}${a.sigma != null ? ` ± ${a.sigma}` : ''}` : showVal(a.value))}</span> ${h(unit(a.unit))}</button>${pageBtn(p, i)}</span>`).join('')}
      <input class="rv-in rv-other num" type="text" data-val="${id}" data-g="${g}" data-fk="val:${id}" data-was="" value="" aria-label="${label}: custom value" placeholder="Other" autocomplete="off"></span>`;
  }
  const choices = (rv.choices || {})[p.id];
  if (choices) {
    return `<select class="rv-in" data-val="${id}" data-g="${g}" data-fk="val:${id}" data-was="${h(p.value ?? '')}" aria-label="${label}">
      ${p.value == null ? '<option value="">Select</option>' : ''}${choices.map(c => `<option value="${h(c)}"${c === p.value ? ' selected' : ''}>${h(showVal(c))}</option>`).join('')}</select>`;
  }
  if (p.kind === 'map' || p.kind === 'mixture') {
    const shown = p.value == null ? 'Edit' : slotValue(p);
    return `<button class="btn sm ghost" type="button" data-rv-open="${id}" aria-expanded="${!!S.rv.open[p.id]}" data-fk="open:${id}">${h(shown)}${ic('down')}</button>`;
  }
  if (p.kind === 'figure') return ro_;
  const text = valueText(p);
  return `<input class="rv-in num" type="text" data-val="${id}" data-g="${g}" data-fk="val:${id}" data-was="${h(text)}" value="${h(text)}" aria-label="${label}" title="${h(text)}" placeholder="Enter value" autocomplete="off">${p.unit ? `<span class="u">${h(unit(p.unit))}</span>` : ''}`;
}
function rvEditor(p, edit) {
  if (!edit || !S.rv.open[p.id] || p.struck) return '';
  const id = h(p.id);
  if (p.kind === 'map') {
    return p.value ? `<div class="rv-sub">${layoutSvg(p, true)}${(p.evidence || []).map(x => `<p class="mut">${h(x)}</p>`).join('')}</div>`
      : `<div class="rv-sub"><span class="edit"><input class="num rv-in" type="text" inputmode="numeric" data-layout-n data-fk="layout-n" aria-label="Pins per side" placeholder="Pins/side"><button class="btn sm" type="button" data-blank-layout>Create blank grid</button></span></div>`;
  }
  if (p.kind === 'mixture') {
    const text = id in S.rv.draft ? S.rv.draft[p.id] : Object.entries(p.value || {}).map(([k, v]) => `${k} ${v}`).join('\n');
    return `<div class="rv-sub"><textarea class="rv-area num" rows="8" data-mix="${id}" data-fk="mix:${id}" aria-label="${h(p.label)}: one nuclide and weight fraction per line" placeholder="Zr90 0.498">${h(text)}</textarea>
      <span class="sl-r">One nuclide and weight fraction per line (sum ~1.0; convert atom densities to weight fractions first).</span>
      <div class="acts in"><button class="btn sm" type="button" data-mix-save="${id}">Save</button><button class="btn sm ghost" type="button" data-mix-reset="${id}">Cancel</button></div></div>`;
  }
  return '';
}
function rvNotes(p, ex) {
  const o = p.struck || p, rv = ex.review || {}, out = [];
  if (o.nature === 'assumed' || o.nature === 'derived') out.push(o.reading);
  if (o.nature === 'ambiguous') out.push((o.alternatives || []).map(a => a.reading).join(' or '));
  if ((o.value == null || o.nature === 'candidate' || (o.flags || []).some(f => ['unit', 'rejected', 'nosigma', 'single'].includes(f))) && o.note) out.push(o.source ? `${o.source}: ${o.note}` : o.note);
  if (p.id in (rv.read || {})) out.push(rv.read[p.id].text ? `Extracted: ${rv.read[p.id].text}` : 'No extracted value');
  if (o.kind === 'added') out.push('Not fed to model');
  return out.filter(Boolean).map(x => `<span class="sl-r">${h(x)}</span>`).join('');
}
function rvRow(p, ex, edit) {
  const rv = ex.review || {}, o = p.struck || p, id = h(p.id), label = h(p.label);
  const ticked = edit && !p.struck && tickedNow(p.id, rv), can = (rv.tickable || []).includes(p.id);
  const tick = !edit || p.struck ? '<span class="rv-tick"></span>'
    : `<input class="rv-tick" type="checkbox" data-tick="${id}" data-fk="tick:${id}" aria-label="Confirm ${label}"${ticked ? ' checked' : ''}${can ? '' : ' disabled title="Fill or select a value first"'}>`;
  const acts = !edit ? '' : p.struck
    ? `<button class="ib xs" type="button" data-rv="extract_unstrike" data-p="${id}" data-fk="undo:${id}" aria-label="Restore ${label}" title="Restore">${ic('undo')}</button>`
    : `${p.id in (rv.read || {}) ? `<button class="ib xs" type="button" data-rv="extract_revert" data-p="${id}" data-fk="undo:${id}" aria-label="Revert ${label}" title="Revert to extracted value">${ic('undo')}</button>` : ''}
       <button class="ib xs rv-x" type="button" data-rv="extract_strike" data-p="${id}" data-fk="del:${id}" aria-label="Strike ${label}" title="${(rv.needed || []).includes(p.id) ? 'Strike (required for run; cannot confirm while struck)' : 'Strike'}">${ic('close')}</button>`;
  return `<li class="rv${S.rv.sel === p.id ? ' on' : ''}${p.struck ? ' struck' : ''}${ticked ? ' ok' : ''}" data-row="${id}">
    ${tick}<div class="rv-name">${natMark(o.nature)}<span class="rv-lb">${p.struck ? `<s>${label}</s>` : label}</span>${rvNotes(p, ex)}</div>
    <div class="rv-val">${rvValue(p, ex, edit)}</div><div class="rv-page">${pageBtn(o.page ? o : (rv.read || {})[p.id] || o, undefined, p.id)}</div><div class="rv-act">${acts}</div>
    ${rvEditor(p, edit)}${edit && !p.struck ? rvDiffs(p, ex) : ''}</li>`;
}

/* ---------- Cross-Check Diffs ---------- */
function checkLabels(cs) {
  const seen = {}, out = {};
  cs.forEach(c => { seen[c.model] = (seen[c.model] || 0) + 1; out[c.id] = seen[c.model] > 1 ? `${c.model} ${seen[c.model]}` : c.model; });
  return out;
}
function scopeNow(cs) {
  const s = S.rv.scope;
  if (s === 'off') return 'off';
  if (s !== 'all' && cs.some(c => c.id === s)) return s;
  return cs.length === 1 ? cs[0].id : 'all';
}
const checksInScope = cs => { const s = scopeNow(cs); return s === 'off' ? [] : cs.filter(c => s === 'all' || c.id === s); };
const srcLink = w => w.page ? `<button class="lnk" type="button" data-src-v="${h(w.src)}" data-fk="src-v:${h(w.src)}" title="View source">p. ${h(w.page)}</button>` : '';
function diffLine(g) {
  const v = g.v, val = `<span class="num rv-dv">${h(showVal(v.show))} <span class="u">${h(unit(v.unit))}</span></span>`;
  const who = w => w.split ? `${w.name} split` : w.name;
  const head = new Set(g.who.map(w => w.page)).size <= 1
    ? `<span class="rv-who">${h(g.who.map(who).join(', '))}</span>${val}${srcLink(g.who[0])}`
    : `${val}${g.who.map(w => `<span class="rv-who">${h(who(w))}</span>${srcLink(w)}`).join('')}`;
  return `<div class="rv-d">${head}${v.nature === 'assumed' ? '<span class="u">assumed</span>' : ''}
    ${v.same ? '' : `<button class="btn sm rv-adopt" type="button" data-adopt="${h(g.adopt)}" data-fk="adopt:${h(g.adopt)}">Adopt</button>`}
    ${v.detail ? `<span class="rv-q rv-more-q">${h(v.detail)}</span>` : ''}${v.quote ? `<q class="rv-q" title="${h(v.quote)}">${h(v.quote)}</q>` : ''}</div>`;
}
function rvDiffs(p, ex) {
  const all = ex.review.checks, cs = checksInScope(all), hits = cs.filter(c => c.rows && c.rows[p.id]);
  if (!hits.length) return '';
  const names = checkLabels(all), groups = new Map(), none = [];
  hits.forEach(c => {
    const d = c.rows[p.id];
    if (d.kind === 'only_list') { none.push(c); return; }
    d.values.forEach((v, i) => {
      if (!groups.has(v.key)) groups.set(v.key, {v, who: [], adopt: `${c.id}|${p.id}|${v.alt ?? ''}`});
      groups.get(v.key).who.push({name: names[c.id], page: v.page, src: `${c.id}|${p.id}|${i}`, split: d.split});
    });
  });
  const agree = cs.filter(c => c.state === 'done' && !hits.includes(c)).map(c => names[c.id]);
  const read = (ex.review.read || {})[p.id];
  const mine = p.nature === 'conflict' || p.value == null ? '' : `<div class="rv-d rv-mine"><span class="rv-who">List</span><span class="num rv-dv">${h(slotValue(p))} <span class="u">${h(p.value && typeof p.value === 'object' ? '' : unit(p.unit))}</span></span>
    ${pageBtn(p.page ? p : read || p, undefined, p.id, 'src-m')}${agree.length ? `<span class="u">matches ${h(agree.join(', '))}</span>` : ''}${p.quote ? `<q class="rv-q" title="${h(p.quote)}">${h(p.quote)}</q>` : ''}</div>`;
  return `<div class="rv-sub rv-ds">${mine}${[...groups.values()].map(diffLine).join('')}
    ${none.map(c => `<div class="rv-d rv-none"><span class="rv-who">${h(names[c.id])}</span><span class="u">Not extracted${c.rows[p.id].note ? `: ${h(c.rows[p.id].note)}` : ''}</span></div>`).join('')}</div>`;
}
function checkChip(c, names, cur) {
  const n = c.rows ? Object.keys(c.rows).length : 0;
  const mark = c.state === 'running' ? glyph('srun', 'Reading') : c.state === 'failed' ? glyph('fail', 'Failed') : c.state === 'stopped' ? glyph('none', 'Stopped') : n ? '' : glyph('pass', 'Match');
  return `<button type="button" data-rv-scope="${h(c.id)}" aria-pressed="${cur === c.id}" title="${h(c.model)}${c.effort !== 'off' ? `, effort ${h(EFFORT[c.effort] || c.effort)}` : ''}">${mark}<span class="rv-cm">${h(names[c.id])}</span>${c.state === 'done' ? `<span class="${n ? 'count num' : 'u'}">${n || 'Match'}</span>` : ''}</button>`;
}
function checksBar(ex) {
  const cs = ex.review.checks;
  if (!cs.length) return '';
  const names = checkLabels(cs), cur = scopeNow(cs), one = cs.find(c => c.id === cur);
  const total = new Set(cs.flatMap(c => Object.keys(c.rows || {}))).size;
  const inScope = checksInScope(cs).some(c => c.rows && Object.keys(c.rows).length);
  const kinds = one && one.rows ? Object.values(one.rows).reduce((a, d) => { a[d.kind] = (a[d.kind] || 0) + 1; return a; }, {}) : {};
  const adoptable = one && one.rows ? Object.values(one.rows).filter(d => d.adoptable).length : 0;
  const running = cs.filter(c => c.state === 'running').length;
  const allTicked = ex.review.need.length && ex.review.need.every(i => tickedNow(i, ex.review));
  const info = one && one.state === 'done' ? [`${one.same} match`, kinds.differ && `${kinds.differ} differ`, kinds.only_check && `${kinds.only_check} check-only`, kinds.only_list && `${kinds.only_list} list-only`].filter(Boolean).join(', ')
    : one && one.state === 'failed' ? one.error : '';
  return `<div class="rv-checks">
    <div class="seg seg-s" role="group" aria-label="Cross-check diffs"><button type="button" data-rv-scope="off" aria-pressed="${cur === 'off'}">Hide</button>
      ${cs.length > 1 ? `<button type="button" data-rv-scope="all" aria-pressed="${cur === 'all'}">All${total ? `<span class="count num">${total}</span>` : ''}</button>` : ''}
      ${cs.map(c => checkChip(c, names, cur)).join('')}</div>
    ${inScope ? `<label class="rv-only"><input type="checkbox" id="rvOnly" data-fk="only"${S.rv.only ? ' checked' : ''}><span>Diffs only</span></label>` : ''}
    ${one ? `<span class="rv-cact">${one.state === 'done' && adoptable ? `<button class="btn sm" type="button" data-adopt-all="${h(one.id)}" title="Adopt ${adoptable} values from this check without overwriting user edits">Adopt all</button>` : ''}
      <button class="btn sm ghost" type="button" data-check-rm="${h(one.id)}">${one.state === 'running' ? 'Stop' : 'Remove'}</button></span>` : ''}
    ${info || (one && one.errors.length) ? `<p class="rv-info">${h(info)}${one.errors.length ? `${info ? '; ' : ''}partial read errors: ${h(one.errors[0])}` : ''}</p>` : ''}
    ${running ? `<p class="rv-info">${running} cross-checks still running (will stop on confirm)</p>` : ''}
    ${allTicked && total ? `<p class="rv-info">${total} diffs vs. cross-checks; confirmation uses list values</p>` : ''}</div>`;
}

/* ---------- Review Toolbar ---------- */
function checkChoice() {
  const main = curChoice();
  let c = null;
  try { c = JSON.parse(localStorage.getItem('p1-check-llm') || 'null'); } catch (_) {}
  const g = c && groupWith(c.group, c.model);
  if (g) return {group: g.id, model: c.model, effort: EFFORT[c.effort] ? c.effort : 'off'};
  for (const x of llmInfo().groups || []) {
    const m = x.models.find(y => y !== main.model);
    if (m) return {group: x.id, model: m, effort: main.effort};
  }
  return main;
}
function setCheckChoice(part) {
  try { localStorage.setItem('p1-check-llm', JSON.stringify(Object.assign(checkChoice(), part))); } catch (_) {}
  S.checkLlmKey = (S.checkLlmKey || 0) + 1;
}
function checkMenu() {
  const c = checkChoice();
  return `<div class="menu mp" role="menu" aria-label="Cross-check model">${(llmInfo().groups || []).map(g => `<div class="menu-h">${h(g.name)}</div>${g.models.map(m => {
    const on = g.id === c.group && m === c.model;
    return `<button type="button" role="menuitemradio" aria-checked="${on}" data-chk-group="${h(g.id)}" data-chk-model="${h(m)}"><span class="mi-m">${h(m)}</span>${on ? ic('check', 'mi-c') : '<span class="mi-c"></span>'}</button>`;
  }).join('')}`).join('')}
    <div class="menu-sep"></div><div class="menu-h">Reasoning effort</div>
    <div class="seg seg-s eff" role="group" aria-label="Reasoning effort">${Object.entries(EFFORT).map(([k, l]) => `<button type="button" data-chk-effort="${k}" aria-pressed="${k === c.effort}">${l}</button>`).join('')}</div></div>`;
}
function reviewBar(ex) {
  const rv = ex.review, done = rv.need.filter(i => tickedNow(i, rv)).length, total = rv.need.length;
  const c = checkChoice(), ready = !ex.blocking.length && total > 0 && done === total;
  return `<div class="rv-bar">
    <label class="rv-all"><input type="checkbox" id="rvAll" data-fk="all"${rv.tickable.length && rv.tickable.every(i => tickedNow(i, rv)) ? ' checked' : ''}><span>All</span></label><span class="num rv-n">${done}/${total}</span>
    <span class="rv-chk"><button class="btn sm" type="button" data-act="rv-check">Cross-check</button><button class="btn sm rv-mp" type="button" data-act="rv-check-model" aria-haspopup="true" aria-label="Cross-check model ${h(c.model)}" title="Cross-check model"><span class="rv-mn">${h(c.model || 'Model')}${c.effort !== 'off' ? ` ${h(EFFORT[c.effort])}` : ''}</span>${ic('down')}</button></span>
    <span class="grow"></span>
    <button class="btn pri" type="button" data-act="extract-confirm"${ready ? '' : ' disabled'}>Confirm & run</button></div>`;
}
function addRowHtml() {
  if (!S.rv.adding) return `<button class="btn sm ghost rv-addb" type="button" data-act="rv-add" data-fk="rv-add">${ic('plus')}<span>Add item</span></button>`;
  const a = S.rv.add, f = (k, ph, cls = '') => `<input class="rv-in ${cls}" type="text" data-add="${k}" data-fk="add:${k}" value="${h(a[k])}" placeholder="${ph}" aria-label="${ph}" autocomplete="off">`;
  return `<div class="rv-addf">${f('label', 'Label')}${f('value', 'Value', 'num')}${f('unit', 'Unit')}${f('page', 'Page', 'num')}
    <button class="btn sm pri" type="button" data-act="rv-add-save">Add</button><button class="btn sm ghost" type="button" data-act="rv-add-cancel">Cancel</button></div>`;
}
function reviewHtml(pl, ex, tail) {
  const edit = ex.state === 'review' && !ro() && !ex.list_only && !!ex.review;
  let rows = rowsOf(pl);
  if (edit && S.rv.only) {
    const cs = checksInScope(ex.review.checks);
    if (cs.some(c => c.rows && Object.keys(c.rows).length)) rows = rows.filter(p => cs.some(c => c.rows && c.rows[p.id]));
  }
  const notes = edit ? ex.blocking : [];
  const warns = edit ? (ex.checks || []).filter(c => c.status === 'warn') : [];
  const reading = ['pending', 'running'].includes(ex.state);
  const errs = edit ? ex.errors || [] : [];
  const failed = ex.state === 'failed' && !ro() ? `<div class="rv-top"><ul class="xnotes rv-block"><li>Extraction failed: ${h(ex.error)}. ${ex.has_read ? 'Existing list and ticks preserved.' : ''}</li></ul>
    <div class="acts in"><button class="btn sm pri" type="button" data-act="extract-retry">Re-extract</button></div></div>` : '';
  return `${failed}${edit ? `<div class="rv-top">${reviewBar(ex)}${checksBar(ex)}${notes.length ? `<ul class="xnotes rv-block">${notes.map(x => `<li>${h(x)}</li>`).join('')}</ul>` : ''}${warns.length ? `<ul class="xnotes rv-block rv-warn">${warns.map(c => `<li>${h(c.text)}</li>`).join('')}</ul>` : ''}${errs.length ? `<ul class="xnotes rv-block rv-warn"><li>Extraction route errors: ${h(errs.join('; '))}</li></ul>` : ''}</div>` : ''}
    ${reading ? `<div class="line">${glyph('srun', 'Reading documents')}<span>Reading documents</span></div>` : ''}
    <ul class="rvl">${rows.map(p => rvRow(p, ex, edit)).join('')}</ul>
    ${edit ? `<div class="rv-more">${addRowHtml()}${S.rv.adding ? '' : '<span class="grow"></span><button class="btn sm ghost" type="button" data-act="extract-retry">Re-extract</button>'}</div>` : ''}
    ${edit ? checksList(ex) : ''}${tail}`;
}
/* ---------- Actions ---------- */
async function startCheck() {
  S.rv.scope = 'all';
  await rvDo('extract_check', checkChoice());
}
async function saveAdded() {
  const a = S.rv.add, page = parseInt(a.page, 10);
  const input = {label: a.label, value: a.value, unit: a.unit};
  if (page > 0) Object.assign(input, {page, doc: S.src ? S.src.hash : S.snap.docs[0].hash});
  try {
    await rpc('task/submit', {goal: S.gid, kind: 'extract_add', input});
    S.rv.add = {label: '', value: '', unit: '', page: ''};
    S.rv.adding = false;
  } catch (e) { toast(e.message); }
  await refresh(false);
}
function openAdd() {
  S.rv.adding = true;
  S.rv.add = {label: '', value: '', unit: '', page: S.src && splitOn() ? String(S.src.page) : ''};
  repaintList().then(() => { const i = $('[data-add="label"]'); if (i) i.focus(); });
}
function saveValue(el) {
  const v = el.value.trim(), was = el.dataset.was;
  if (v === was || el.dataset.sent === v) return;
  el.dataset.sent = v;
  const rv = S.rv, gesture = rv.gesture;
  const saving = rvDo('extract_fill', {param: el.dataset.val, value: v}, () => { el.value = was; delete el.dataset.sent; rv.failed = gesture; }, el.dataset.g);
  rv.saving = saving;
  saving.then(ok => {
    if (rv.saving === saving) rv.saving = null;
    if (ok && el.isConnected && document.activeElement !== el && el.dataset.was === was) { el.value = was; delete el.dataset.sent; }
  });
}
function checkValue(key) {
  const [cid, pid, i] = key.split('|');
  const c = S.snap.extraction.review.checks.find(x => x.id === cid);
  return c && c.rows[pid] && c.rows[pid].values[+i];
}
function noticeChecks() {
  const ex = S.snap && S.snap.extraction, cs = ex && ex.review ? ex.review.checks : [];
  const names = checkLabels(cs);
  cs.forEach(c => {
    if (S.rv.seen[c.id] === 'running' && c.state === 'done') {
      const n = Object.keys(c.rows).length;
      toast(`${names[c.id]}: ${n ? `${n} diffs vs. list` : 'matches list'}`);
    } else if (S.rv.seen[c.id] === 'running' && c.state === 'failed') toast(`${names[c.id]}: ${c.error}`);
    S.rv.seen[c.id] = c.state;
  });
}
