'use strict';
/* LLM selector, streaming reasoning blocks, and provider settings dialog */

const EFFORT = {off: 'Off', low: 'Low', medium: 'Med', high: 'High', max: 'Max'};
const PROVIDER = {openai: 'OpenAI', anthropic: 'Anthropic'};
const URL_HINT = {openai: 'https://api.openai.com/v1', anthropic: 'https://api.anthropic.com'};
/* Use masked text input instead of type="password" where supported to avoid browser password manager prompts */
const MASK = CSS.supports('-webkit-text-security', 'disc');

/* ---------- Model & Effort Picker ---------- */
function llmInfo() { return (S.health && S.health.llm) || {groups: [], default: {group: '', model: '', effort: 'off'}}; }
function groupWith(gid, model) {
  return (llmInfo().groups || []).find(g => (!gid || g.id === gid) && g.models.includes(model));
}
function newChoice() {
  const d = llmInfo().default || {}, c = S.newLlm || {}, g = groupWith(c.group, c.model);
  return {group: g ? g.id : d.group || '', model: g ? c.model : d.model || '', effort: EFFORT[c.effort] ? c.effort : 'off'};
}
function curChoice() {
  if (!S.snap) return newChoice();
  return S.llmWant || (S.snap.llm && S.snap.llm.choice) || newChoice();
}
function curGroupName() {
  const g = (llmInfo().groups || []).find(x => x.id === curChoice().group);
  return g ? g.name : '';
}
function renderPick() {
  const b = $('#mpick'), c = curChoice();
  const html = c.model ? `<span class="mp-m">${h(c.model)}</span>${c.effort !== 'off' ? `<span class="mp-e">${EFFORT[c.effort]}</span>` : ''}${ic('down')}`
    : `<span class="mp-m mut">Model</span>${ic('down')}`;
  if (b.dataset.h !== html) { b.innerHTML = html; b.dataset.h = html; }
  b.disabled = ro();
  b.setAttribute('aria-label', c.model ? `Model ${c.model}, reasoning effort ${EFFORT[c.effort]}` : 'Select model');
}
function pickMenu() {
  const c = curChoice(), groups = llmInfo().groups || [];
  if (!groups.length) return `<div class="menu" role="menu"><button type="button" role="menuitem" data-act="llm-settings">Model API settings</button></div>`;
  return `<div class="menu mp" role="menu" aria-label="Model and reasoning effort">
    ${groups.map(g => `<div class="menu-h">${h(g.name)}</div>${g.models.map(m => {
      const on = g.id === c.group && m === c.model;
      return `<button type="button" role="menuitemradio" aria-checked="${on}" data-llm-group="${h(g.id)}" data-llm-model="${h(m)}"><span class="mi-m">${h(m)}</span>${on ? ic('check', 'mi-c') : '<span class="mi-c"></span>'}</button>`;
    }).join('')}`).join('')}
    <div class="menu-sep"></div><div class="menu-h">Reasoning effort</div>
    <div class="seg seg-s eff" role="group" aria-label="Reasoning effort">${Object.entries(EFFORT).map(([k, l]) =>
      `<button type="button" data-llm-effort="${k}" aria-pressed="${k === c.effort}">${l}</button>`).join('')}</div></div>`;
}
async function setChoice(part) {
  const c = Object.assign({}, curChoice(), part);
  if (!S.snap) {
    S.newLlm = c;
    try { localStorage.setItem('p1-llm', JSON.stringify(c)); } catch (_) {}
    renderPick();
    return;
  }
  S.llmWant = c; renderPick();
  try { await rpc('task/submit', {goal: S.gid, kind: 'llm', input: c}); }
  catch (e) { toast(e.message); }
  S.llmWant = null;
  await refresh(false);
}

/* ---------- Context Usage: total tokens of last completed call ---------- */
function contextK() {
  const calls = ((S.snap && S.snap.llm_calls) || []).filter(c => c.status === 'ok' && c.purpose !== 'check');
  const c = calls[calls.length - 1];
  return ((c ? c.prompt_tokens + c.completion_tokens : 0) / 1000).toFixed(1) + 'k';
}

/* ---------- Thinking Blocks ---------- */
function callsById() {
  const m = {};
  ((S.snap && S.snap.llm_calls) || []).forEach(c => { m[c.id] = c; });
  return m;
}
/* Completed calls: collapsed by default, lazy-load reasoning text on expand */
function thinkBlock(ids, key) {
  if (!S.showThink || !S.snap) return '';
  const by = callsById();
  const cs = (ids || []).map(i => by[i]).filter(c => c && c.reasoning_chars > 0);
  if (!cs.length) return '';
  const secs = cs.reduce((a, c) => a + (c.think_s || 0), 0);
  const k = 'th:' + key, open = opened(k);
  const body = open ? cs.map(c => S.thinkText[c.id] == null ? '' : h(S.thinkText[c.id])).join('\n\n') : '';
  return `<details class="think" data-k="${h(k)}" data-think="${h(cs.map(c => c.id + ':' + c.response).join(','))}"${open ? ' open' : ''}>
    <summary><span class="th-h">Thought for ${Math.max(1, Math.round(secs))} s</span>${ic('chev', 'tw')}</summary>
    ${open ? `<div class="th-b">${body || '<span class="mut">...</span>'}</div>` : ''}</details>`;
}
/* In-flight calls: expanded live while streaming */
function liveBlock(purposes) {
  if (!S.showThink) return '';
  const lv = Object.entries(S.live).filter(([, v]) => purposes.includes(v.purpose) && v.text);
  return lv.map(([id, v]) => `<details class="think live" open data-live="${h(id)}"><summary><span class="th-h">Thinking ${Math.round((Date.now() - v.t0) / 1000)} s</span>${ic('chev', 'tw')}</summary>
    <div class="th-b">${h(v.text)}</div></details>`).join('');
}
function onThinkDelta(p) {
  if (p.done) { delete S.live[p.call]; softRefresh(); return; }
  const v = S.live[p.call] || (S.live[p.call] = {purpose: p.purpose, text: '', t0: Date.now()});
  v.text += p.reasoning || '';
  const el = $(`[data-live="${p.call}"]`);
  if (!el) { renderThread(); return; }
  const b = $('.th-b', el), stick = b.scrollTop + b.clientHeight >= b.scrollHeight - 8;
  b.textContent = v.text;
  if (stick) b.scrollTop = b.scrollHeight;
}
setInterval(() => {
  $$('[data-live]').forEach(el => {
    const v = S.live[el.dataset.live];
    if (v) $('.th-h', el).textContent = `Thinking ${Math.round((Date.now() - v.t0) / 1000)} s`;
  });
}, 1000);
async function loadThink(el) {
  const parts = (el.dataset.think || '').split(',').map(x => x.split(':')).filter(([id]) => S.thinkText[id] == null);
  if (!parts.length && $('.th-b', el)) return;
  await Promise.all(parts.map(async ([id, resp]) => {
    try { S.thinkText[id] = (await api(`api/blob/${resp}`)).reasoning || ''; } catch (_) { S.thinkText[id] = ''; }
  }));
  const sc = $('#scroll'), top = sc.scrollTop;
  S.threadHtml = null;
  renderThread();
  sc.scrollTop = top;
}

/* ---------- Model API Settings Dialog ---------- */
function formOf(g) {
  if (!g) return {id: null, name: '', enabled: true, provider: '', base_url: '', key: '', params: '', models: [], catalog: []};
  return {id: g.id, name: g.name, enabled: g.enabled, provider: g.provider, base_url: g.base_url, key: '',
    params: Object.keys(g.params || {}).length ? JSON.stringify(g.params, null, 1) : '',
    models: [...g.models], catalog: [...new Set([...(g.catalog || []), ...g.models])]};
}
const dlgNarrow = () => matchMedia('(max-width: 640px)').matches;
const formKey = f => JSON.stringify([f.name.trim(), f.enabled, f.provider, f.base_url.trim().replace(/\/+$/, ''), f.params.trim(), [...f.models].sort()]);
async function dlgSync(sel, keep) {
  const s = await api('api/llm/settings');
  const g = s.groups.find(x => x.id === sel);
  if (sel !== 'new' && !g) sel = dlgNarrow() || !s.groups.length ? (s.groups.length ? null : 'new') : s.groups[0].id;
  const cur = s.groups.find(x => x.id === sel);
  S.dlg = {groups: s.groups, sel, form: formOf(cur), base: cur ? {provider: cur.provider, base_url: cur.base_url} : null,
    keySet: !!(cur && cur.key_set), keyShown: false, keyOrig: '', add: '', test: null, fetchErr: '', err: '', busy: '', sure: false};
  S.dlg.orig = formKey(S.dlg.form);
  Object.assign(S.dlg, keep || {});
  renderDlg();
}
async function openLlmDialog() {
  try { await dlgSync(dlgNarrow() ? null : curChoice().group || ''); } catch (e) { toast(e.message); return; }
  $('#llmDlg').showModal();
}
function addrKept() {
  const d = S.dlg, f = d.form;
  return !!d.base && f.provider === d.base.provider && f.base_url.trim().replace(/\/+$/, '') === d.base.base_url;
}
/* Revoke revealed key if provider or base URL changes to prevent exfiltration */
function dropShownKey() {
  const d = S.dlg;
  if (addrKept() || !d.keyOrig || d.form.key !== d.keyOrig) return false;
  d.form.key = d.keyOrig = '';
  d.keyShown = false;
  return true;
}
function keyPlaceholder() { return S.dlg.keySet && addrKept() ? '••••••••••••••••' : ''; }
function dlgDirty() {
  const d = S.dlg;
  return d.sel === 'new' || formKey(d.form) !== d.orig || d.form.key !== d.keyOrig;
}
function dlgButtons() {
  const dirty = dlgDirty(), busy = S.dlg.busy;
  const sv = $('[data-dlg="save"]'), cn = $('[data-dlg="cancel"]');
  if (sv) sv.disabled = !dirty || !!busy;
  if (cn) cn.disabled = !dirty;
}
function groupRows() {
  const d = S.dlg;
  const row = (id, name, on) => `<button class="gr ${d.sel === id ? 'on' : ''}" type="button" data-dlg-g="${h(id)}"${d.sel === id ? ' aria-current="true"' : ''}><i class="gd ${on ? 'ok' : ''}" aria-hidden="true"></i><span class="gr-n ${name ? '' : 'mut'}">${h(name || 'Untitled')}</span></button>`;
  return d.groups.map(g => row(g.id, d.sel === g.id ? d.form.name : g.name, d.sel === g.id ? d.form.enabled : g.enabled)).join('')
    + (d.sel === 'new' ? row('new', d.form.name, d.form.enabled) : '');
}
function renderDlg() {
  const d = S.dlg, f = d.form, busy = d.busy;
  const old = $('#llmDlg .dlg-b'), top = old ? old.scrollTop : 0;
  const res = d.test ? (d.test.ok ? `${glyph('pass', 'Connected')}<span class="num">${d.test.seconds} s</span>` : `${glyph('fail', 'Connection failed')}<span>${h(d.test.err)}</span>`)
    : busy === 'test' ? glyph('srun', 'Testing') : '';
  const form = d.sel == null ? '' : `<header class="dlg-h"><button class="ib dlg-back" type="button" data-dlg="back" aria-label="Back to groups" title="Back to groups">${ic('back')}</button></header>
    <div class="dlg-b">
      <div class="fr"><label class="fl" for="dName">Name</label><input id="dName" class="fi" data-dlg-f="name" value="${h(f.name)}" spellcheck="false" autocomplete="off"></div>
      <div class="fr"><span class="fl" id="dEnL">Enabled</span><div><button class="swb" type="button" role="switch" aria-labelledby="dEnL" aria-checked="${f.enabled}" data-dlg="enabled"><i class="sw" aria-hidden="true"></i></button></div></div>
      <div class="fr"><span class="fl">Provider</span><div class="seg seg-s" role="group" aria-label="Provider">${Object.entries(PROVIDER).map(([k, l]) =>
        `<button type="button" data-dlg-prov="${k}" aria-pressed="${f.provider === k}">${l}</button>`).join('')}</div></div>
      <div class="fr"><label class="fl" for="dUrl">API URL</label><input id="dUrl" class="fi mono" data-dlg-f="base_url" value="${h(f.base_url)}" placeholder="${URL_HINT[f.provider] || ''}" spellcheck="false" autocomplete="off"></div>
      <div class="fr"><label class="fl" for="dKey">API Key</label><div class="fkey"><input id="dKey" class="fi mono ${!d.keyShown && MASK ? 'fmask' : ''}" type="${d.keyShown || MASK ? 'text' : 'password'}" data-dlg-f="key" value="${h(f.key)}"
        placeholder="${keyPlaceholder()}" spellcheck="false" autocomplete="off">
        <button class="ib" type="button" data-dlg="eye" aria-pressed="${d.keyShown}" aria-label="${d.keyShown ? 'Hide key' : 'Show key'}" title="${d.keyShown ? 'Hide key' : 'Show key'}">${ic(d.keyShown ? 'eyeoff' : 'eye')}</button></div></div>
      <div class="fr"><span class="fl"></span><div class="frow"><button class="btn" type="button" data-dlg="test" ${busy ? 'disabled' : ''}>Test connection</button><span class="dres">${res}</span></div></div>
      <div class="fr ftop"><span class="fl">Models</span><div class="fcol">
        <div class="frow"><button class="btn" type="button" data-dlg="fetch" ${busy ? 'disabled' : ''}>Fetch model list</button><span class="dres">${busy === 'fetch' ? glyph('srun', 'Fetching') : d.fetchErr ? `${glyph('fail', 'Fetch failed')}<span>${h(d.fetchErr)}</span>` : ''}</span></div>
        ${f.catalog.length ? `<ul class="mchk">${f.catalog.map(m => `<li><label><input type="checkbox" data-dlg-m="${h(m)}" ${f.models.includes(m) ? 'checked' : ''}><span class="mono">${h(m)}</span></label></li>`).join('')}</ul>` : ''}
        <div class="frow"><input class="fi mono" data-dlg-f="add" value="${h(d.add)}" placeholder="Model ID" aria-label="Model ID" spellcheck="false" autocomplete="off"><button class="btn" type="button" data-dlg="add">Add</button></div>
      </div></div>
      <div class="fr ftop"><label class="fl" for="dPar">Custom params</label><textarea id="dPar" class="fi mono" data-dlg-f="params" rows="3" spellcheck="false" placeholder='{"temperature": 0.2}'>${h(f.params)}</textarea></div>
      ${d.err ? `<p class="derr" role="alert">${h(d.err)}</p>` : ''}
    </div>
    <footer class="dlg-a">${d.sel !== 'new' ? `<button class="btn ${d.sure ? 'danger' : ''}" type="button" data-dlg="delete">${d.sure ? 'Confirm delete' : 'Delete'}</button>` : ''}<span class="grow"></span>
      <button class="btn" type="button" data-dlg="cancel">Cancel</button><button class="btn pri" type="button" data-dlg="save">Save</button></footer>`;
  $('#llmDlg').innerHTML = `<div class="dlg-w ${d.sel != null ? 'sel' : ''}">
    <div class="dlg-l"><h2 id="llmT">Model APIs</h2><nav class="grl" id="grl" aria-label="Groups">${groupRows()}</nav>
      <button class="btn dlg-new" type="button" data-dlg="new">${ic('plus')}<span>New</span></button></div>
    <div class="dlg-r">${form}</div>
    <button class="ib dlg-x" type="button" data-dlg="close" aria-label="Close" title="Close">${ic('close')}</button></div>`;
  const nb = $('#llmDlg .dlg-b');
  if (nb) nb.scrollTop = top;
  dlgButtons();
}
function dlgParams() {
  const t = S.dlg.form.params.trim();
  if (!t) return {};
  const v = JSON.parse(t);
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('Custom params must be a JSON object');
  return v;
}
function dlgBody(params) {
  const f = S.dlg.form;
  const body = {name: f.name, enabled: f.enabled, provider: f.provider, base_url: f.base_url.trim(),
    models: f.catalog.filter(m => f.models.includes(m)), catalog: f.catalog, params};
  if (f.id) body.id = f.id;
  if (f.key.trim() && f.key !== S.dlg.keyOrig) body.key = f.key.trim();
  return body;
}
const llmPost = (path, b) => api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
async function afterLlmChange() {
  try { S.health = await api('api/health'); } catch (_) {}
  renderAvatar(); renderPick();
  if (S.gid) await refresh(false);
}
async function dlgAct(a) {
  const d = S.dlg, f = d.form;
  if (a !== 'delete') d.sure = false;
  try {
    if (a === 'close') { $('#llmDlg').close(); return; }
    if (a === 'new') { await dlgSync('new'); $('#dName').focus(); return; }
    if (a === 'back') { await dlgSync(null); return; }
    if (a === 'cancel') { await dlgSync(dlgNarrow() ? null : d.sel === 'new' ? '' : d.sel); return; }
    if (a === 'enabled') { f.enabled = !f.enabled; renderDlg(); return; }
    if (a === 'eye') {
      if (!d.keyShown && !f.key && d.keySet && addrKept()) {
        const r = await llmPost('api/llm/key', {id: f.id});
        f.key = d.keyOrig = r.key;
      }
      d.keyShown = !d.keyShown; renderDlg(); $('#dKey').focus(); return;
    }
    if (a === 'add') {
      const m = d.add.trim();
      if (!m) { $('[data-dlg-f="add"]').focus(); return; }
      if (!f.catalog.includes(m)) f.catalog.push(m);
      if (!f.models.includes(m)) f.models.push(m);
      d.add = ''; renderDlg(); return;
    }
    if (a === 'delete') {
      if (!d.sure) { d.sure = true; renderDlg(); return; }
      await llmPost('api/llm/groups/delete', {id: f.id});
      await dlgSync(dlgNarrow() ? null : '');
      await afterLlmChange();
      return;
    }
  } catch (e) { d.err = e.message; renderDlg(); return; }
  let params;
  try { params = dlgParams(); } catch (e) { d.err = e instanceof SyntaxError ? 'Custom params is not valid JSON' : e.message; renderDlg(); return; }
  d.err = '';
  const body = dlgBody(params);
  if (a === 'test') {
    d.busy = 'test'; d.test = null; renderDlg();
    try { d.test = Object.assign({ok: true}, await llmPost('api/llm/test', Object.assign(body, {model: body.models[0] || ''}))); }
    catch (e) { d.test = {ok: false, err: e.message}; }
    d.busy = ''; renderDlg(); return;
  }
  if (a === 'fetch') {
    d.busy = 'fetch'; d.fetchErr = ''; renderDlg();
    try {
      const r = await llmPost('api/llm/models', body);
      const none = !f.models.length;
      f.catalog = [...new Set([...r.models, ...f.catalog.filter(m => f.models.includes(m))])];
      if (none) f.models = [...r.models];
    } catch (e) { d.fetchErr = e.message; }
    d.busy = ''; renderDlg(); return;
  }
  if (a === 'save') {
    if (d.keyShown && d.keySet && !f.key.trim()) { d.err = 'API key is required'; renderDlg(); return; }
    d.busy = 'save'; renderDlg();
    try {
      const r = await llmPost('api/llm/groups', body);
      await dlgSync(dlgNarrow() ? null : r.saved);
      await afterLlmChange();
    } catch (e) { d.err = e.message; d.busy = ''; renderDlg(); }
  }
}
document.addEventListener('input', ev => {
  const n = ev.target.dataset && ev.target.dataset.dlgF, d = S.dlg;
  if (!n || !d) return;
  if (n === 'add') { d.add = ev.target.value; return; }
  d.form[n] = ev.target.value;
  if (n === 'name') { const r = $('#grl'); if (r) r.innerHTML = groupRows(); }
  if (n === 'base_url') {
    if (dropShownKey()) {
      const pos = ev.target.selectionStart;
      renderDlg();
      const u = $('#dUrl');
      u.focus(); u.setSelectionRange(pos, pos);
      return;
    }
    const k = $('#dKey');
    if (k) k.placeholder = keyPlaceholder();
  }
  dlgButtons();
});
document.addEventListener('change', ev => {
  const m = ev.target.dataset && ev.target.dataset.dlgM, d = S.dlg;
  if (m == null || !d) return;
  const f = d.form;
  f.models = ev.target.checked ? [...f.models, m] : f.models.filter(x => x !== m);
  dlgButtons();
});
document.addEventListener('keydown', ev => {
  if (ev.key === 'Enter' && ev.target.dataset && ev.target.dataset.dlgF === 'add') { ev.preventDefault(); dlgAct('add'); }
});
