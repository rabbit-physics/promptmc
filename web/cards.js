'use strict';
/* Thread stream: renders messages and live cards in snapshot.chat order, appending confirmation and progress cards */

function goalState(sn) {
  return sn ? sn.state : null;
}
function isRunning(sn) {
  if (!sn || sn.goal.archived) return false;
  const busy = list => list && ['running', 'queued', 'stopping'].includes(list.status);
  if (sn.cases.some(c => busy(c.ref_mc))) return true;
  if (sn.goal.status !== 'active') return false;
  return sn.cases.some(c => busy(c.task)) || (!sn.goal.paused && sn.cases.some(c => !c.result) && !sn.acceptance);
}

/* ---------- Thread Stream ---------- */
function threadHtml() {
  const sn = S.snap;
  const out = [];
  if (!sn) {
    (S.temp || []).forEach(m => out.push(m.role === 'user' ? userMsg(m.text, m.docs) : botMsg(prose(m.text) + (m.goal ? `<p><a class="lnk" href="#g=${h(m.goal)}" data-goal="${h(m.goal)}">Open goal</a></p>` : ''), m.err ? 'err' : '')));
  } else {
    const rows = sn.chat;
    const cards = rows.map(r => meta(r).card).filter(Boolean);
    /* Collapse earlier cards of the same live type, keeping only the latest expanded */
    S.lastCard = {};
    rows.forEach(r => { const k = meta(r).card; if (LIVE[k]) S.lastCard[k] = r.id; });
    /* Keep only the most recent report card if regenerated */
    S.lastReport = null;
    rows.forEach(r => { if (meta(r).report || meta(r).card === 'report') S.lastReport = r.id; });
    const legacy = !cards.length;
    const used = {approvals: new Set(), decisions: new Set(), groups: new Set()};
    const firstReport = rows.findIndex(r => meta(r).report || meta(r).card === 'report');
    rows.forEach((r, i) => {
      if (legacy && i === firstReport) out.push(legacyTail());
      out.push(rowHtml(r, i, rows, used));
    });
    if (legacy && firstReport < 0) out.push(legacyTail());
    const pendA = sn.approvals.filter(a => a.status === 'pending' && !used.approvals.has(a.id));
    const restA = pendA.filter(a => !(a.kind === 'binding' && used.groups.has(a.payload.solver)));
    if (restA.length) out.push(botMsg(restA.map(a => a.kind === 'binding' ? groupFor(a, used) : approvalCard(a)).join('')));
    const openD = sn.decisions.filter(d => d.status === 'open' && !used.decisions.has(d.id));
    if (openD.length) out.push(botMsg(openD.map(decisionCard).join('')));
    if (sn.goal.status === 'ready' && (goalMode(sn) === 'chat' || sn.goal.mode === 'ask')) out.push(botMsg(confirmCard()));
    if (sn.goal.status === 'active' && !cards.includes('runs') && sn.cases.some(c => !c.result)) out.push(botMsg(runsCard()));
  }
  const live = S.pending && S.snap ? liveBlock(['assist', 'parse_edit', 'answer']) : '';
  const last = sn && sn.chat[sn.chat.length - 1];
  const echoed = last && last.role === 'user' && last.text === S.pending;
  const reading = sn && sn.extraction && ['pending', 'running'].includes(sn.extraction.state);
  if (reading && !S.pending) out.push('<div class="msg bot"><div class="typing" role="status" aria-label="Reading files"><i></i><i></i><i></i></div></div>');
  if (S.pending) out.push((echoed ? '' : userMsg(S.pending, S.pendingDocs)) + (live ? botMsg(live) : `<div class="msg bot"><div class="typing" role="status" aria-label="Waiting for reply"><i></i><i></i><i></i></div></div>`));
  return out.join('');
}
function userMsg(text, docs) {
  const chips = (docs || []).length ? `<div class="atts sent">${docs.map(d => `<span class="att" title="${h(d.name)}">${d.hash && d.kind === 'image' ? `<img src="api/docs/${d.hash}/page/1.png" alt="">` : ic('doc')}<span class="att-n">${h(d.name)}</span></span>`).join('')}</div>` : '';
  return `<div class="msg user">${chips}${String(text || '').trim() ? `<div class="bubble">${h(text).replace(/\n/g, '<br>')}</div>` : ''}</div>`;
}
function botMsg(inner, cls = '') { return inner ? `<div class="msg bot ${cls}">${inner}</div>` : ''; }

function rowHtml(r, i, rows, used) {
  const sn = S.snap, m = meta(r);
  if (r.role === 'user') return userMsg(r.text, m.docs);
  if ((m.report || m.card === 'report') && r.id !== S.lastReport) return '';
  if (m.approval) {
    const a = sn.approvals.find(x => x.id === m.approval);
    if (!a) return botMsg(prose(r.text), r.role);
    used.approvals.add(a.id);
    if (a.status === 'pending') {
      if (a.kind === 'binding') return botMsg(groupFor(a, used), r.role);
      return botMsg(approvalCard(a), r.role);
    }
    return botMsg(doneLine(a), r.role);
  }
  if (m.decision) {
    const d = sn.decisions.find(x => x.id === m.decision);
    if (!d) return botMsg(prose(r.text), r.role);
    used.decisions.add(d.id);
    if (d.status === 'open') return botMsg(decisionCard(d), r.role);
    const void_ = d.answer && d.answer.option === 'superseded';
    return botMsg(`<div class="line">${void_ ? glyph('info', 'Superseded') : glyph('pass', 'Answered')}<span>${h(d.question)}${void_ ? ` (superseded: ${h(d.answer.text || '')})` : ''}</span></div>`, r.role);
  }
  const liveList = m.card === 'extract' && m.calls && S.lastCard.extract === r.id && S.snap.extraction && S.snap.extraction.state !== 'failed' && S.snap.extraction.summary;
  let body = (m.calls ? thinkBlock(m.calls, r.id) : '') + prose(liveList || r.text);
  const p = m.parse || {};
  if (p.kind === 'modify' && m.preview) body += modifyCard(r, rows, i);
  if (p.kind === 'reading') body += readingCard(r, rows, i);
  if (m.proposal) body += proposalCard(m.proposal);
  if (m.trials) body += m.trials.map(trialCard).join('');
  if (m.work) body += workCard(m.work);
  if (m.card) body += cardHtml(m, r);
  else if (m.report) body += reportCard(m.report);
  return botMsg(body, r.role);
}
function legacyTail() {
  const sn = S.snap;
  if (!sn.cases.some(c => c.result)) return '';
  return botMsg(resultsCard() + (sn.acceptance ? acceptanceCard() : ''));
}
const LIVE = {params: 'Parameters', extract: 'Parameters', runs: 'Runs', results: 'Results', acceptance: 'Acceptance'};
function cardHtml(m, r) {
  if (LIVE[m.card] && S.lastCard[m.card] !== r.id) return `<div class="card old"><div class="card-h">${glyph('none', 'Superseded by newer version')}<b>${LIVE[m.card]}</b></div></div>`;
  switch (m.card) {
    case 'params': return paramsCard(r);
    case 'extract': return extractCard(r);
    case 'checks': return checksCard(m.run);
    case 'runs': return runsCard();
    case 'results': return resultsCard();
    case 'acceptance': return S.snap.acceptance ? acceptanceCard() : '';
    case 'report': return reportCard(m.report || m.hash);
  }
  return '';
}
const opened = k => !!S.open[k];
const caseChip = c => `<button class="chip-case" type="button" data-panel="input" data-case="${h(c)}" title="${h(caseLabel(c))}">${h(c)}</button>`;
function caseLabel(c) { const x = S.snap && S.snap.cases.find(y => y.case === c); return x ? `${c} ${x.label}` : c; }

/* ---------- Approvals ---------- */
const DONE_A = {approved: ['pass', 'Approved'], rejected: ['fail', 'Rejected'], withdrawn: ['info', 'Withdrawn']};
function doneLine(a) {
  const [k, l] = DONE_A[a.status] || ['info', a.status];
  return `<div class="line">${glyph(k, l)}<span>${h(a.title)}</span></div>`;
}
function bindingItems(p) {
  return `<ul class="items">${p.items.map(it => `<li><span class="it-k">${h(it.what)}</span><span class="it-v">${h(it.choice)}${it.status === 'approx' ? glyph('warn', 'Approximation') : ''}
      ${it.detail ? `<span class="it-d">${h(it.detail)}</span>` : ''}
      ${it.alternatives && it.alternatives.length && !ro() ? `<select data-alt="${h(p.solver)}" aria-label="Change binding">
        <option value="">${h(it.choice)}</option>${it.alternatives.map(o => `<option value="${h(o.choice)}" data-k="${h(o.key || it.id)}">${h(o.label)}</option>`).join('')}</select>` : ''}</span></li>`).join('')}</ul>`;
}
const SOLVER = {openmc: 'OpenMC 0.15.3', ref_mc: 'Reference MC 1.14'};
function groupFor(a, used) {
  const solver = a.payload.solver;
  if (used.groups.has(solver)) return '';
  used.groups.add(solver);
  const list = S.snap.approvals.filter(x => x.status === 'pending' && x.kind === 'binding' && x.payload.solver === solver);
  list.forEach(x => used.approvals.add(x.id));
  const n = list.reduce((s, x) => s + x.payload.items.length, 0);
  const k = `grp:${solver}`;
  return `<div class="card">
    <div class="card-h">${glyph('wait', 'Pending approval')}<b>${h(SOLVER[solver])} bindings</b><span class="count num">${n}</span>
      <span class="cases">${list.map(x => caseChip(x.case_id)).join('')}</span></div>
    <details data-k="${k}"${opened(k) ? ' open' : ''}><summary>${ic('chev', 'tw')}<span>${list.length} cases</span></summary>
      ${list.map(x => `<h4>${h(x.case_id)}</h4>${bindingItems(x.payload)}`).join('')}</details>
    ${ro() ? '' : `<div class="acts"><button class="btn pri" data-approve="${h(list.map(x => x.id).join(','))}" type="button">Approve</button>
      <button class="btn" data-reject="${h(list.map(x => x.id).join(','))}" type="button">Reject</button></div>`}</div>`;
}
function approvalCard(a) {
  const p = a.payload;
  let body = '';
  if (a.kind === 'binding') body = bindingItems(p);
  else if (a.kind === 'run') {
    const e = p.estimate || {};
    body = `<div class="kvs"><span>Estimated wall time</span><b class="num">${dur(e.wall_s || 0)}</b></div>
      <div class="cases">${(p.cases || []).map(caseChip).join('')}</div>`;
  } else if (a.kind === 'param_change' && p.changes) {
    body = `<ul class="items">${p.changes.map(c => `<li><span class="it-k">${h(c.label)}</span><span class="it-v num">${h(c.old)} → ${h(c.new)} ${h(unit(c.unit))}</span></li>`).join('')}</ul>`;
  }
  return `<div class="card"><div class="card-h">${glyph('wait', 'Pending approval')}<b>${h(a.title)}</b></div>${body}
    ${ro() ? '' : `<div class="acts"><button class="btn pri" data-approve="${h(a.id)}" type="button">Approve</button>
    <button class="btn" data-reject="${h(a.id)}" type="button">Reject</button></div>`}</div>`;
}

/* ---------- Decisions ---------- */
function decisionCard(d) {
  const grp = d.options.find(o => o.action && o.action.cases);
  const cs = grp ? grp.action.cases : d.case_id ? [d.case_id] : [];
  return `<div class="card"><div class="card-h">${glyph('wait', 'Decision required')}<b>${h(d.question)}</b>
      ${cs.length ? `<span class="cases">${cs.map(caseChip).join('')}</span>` : ''}</div>
    ${ro() ? '' : `<div class="acts wrap">${d.options.map(o => o.input
      ? `<span class="optin"><label><span>${h(o.label)}</span><input class="num" type="number" step="any" data-dval="${h(d.id)}:${h(o.id)}" value="${h(S.dvals[`${d.id}:${o.id}`] || '')}"> ${h(unit(o.input.unit || ''))}</label>
         <button class="btn" data-dopt="${h(d.id)}:${h(o.id)}" type="button">Submit</button></span>`
      : `<button class="btn ${o.id === 'resume' || o.id.startsWith('alt') ? 'pri' : ''}" data-dopt="${h(d.id)}:${h(o.id)}" type="button">${h(o.label)}</button>`).join('')}</div>`}</div>`;
}

/* ---------- Parameter & Reading Edit Previews ---------- */
function prevUserId(rows, i) { for (let j = i - 1; j >= 0; j--) if (rows[j].role === 'user') return rows[j].id; return null; }
function laterEdit(rows, i) { return rows.slice(i + 1).some(r => { const k = (meta(r).parse || {}).kind; return k === 'modify' || k === 'reading'; }); }
function modifyCard(r, rows, i) {
  const uid = prevUserId(rows, i);
  const mods = S.snap.modifications || [];
  if (mods.some(m => m.edit && m.edit.chat === uid) || laterEdit(rows, i) || ro()) return '';
  const changes = {};
  (meta(r).parse.changes || []).forEach(c => { changes[c.param] = +c.value; });
  if (!Object.keys(changes).length) return '';
  const pn = S.nodes[S.snap.goal.spec.paramlist];
  if (!pn) { node(S.snap.goal.spec.paramlist).then(renderThread).catch(() => {}); return ''; }
  const cur = {}; pn.content.params.forEach(p => { cur[p.id] = p.value; });
  if (Object.entries(changes).every(([k, v]) => typeof cur[k] === 'number' && Math.abs(cur[k] - v) <= 1e-9 * Math.max(1, Math.abs(v)))) return '';
  const pv = S.previews[r.id];
  if (!pv) { loadPreview(r.id, changes); return `<div class="card"><div class="card-h">${glyph('srun', 'Generating preview')}<b>Edit preview</b></div></div>`; }
  if (pv.error) return `<div class="card"><div class="card-h">${glyph('fail', 'Preview failed')}<b>Edit preview</b></div><p class="mut">${h(pv.error)}</p></div>`;
  return `<div class="card">${previewBody(pv, `pv:${r.id}`)}
    <div class="acts"><button class="btn pri" data-apply="${r.id}" data-uid="${uid ?? ''}" type="button" ${pv.cases.length ? '' : 'disabled'}>Apply</button></div></div>`;
}
async function loadPreview(rid, changes) {
  if (S.previews[rid] !== undefined) return;
  S.previews[rid] = null;
  try {
    const pv = await api('api/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({goal: S.gid, changes})});
    S.previews[rid] = Object.assign(pv, {changes});
  } catch (e) { S.previews[rid] = {error: e.message}; }
  renderThread();
}
function diffPre(lines, label) {
  return `<pre class="diff" aria-label="${h(label)}">${lines.map(l => {
    const c = l.startsWith('+') && !l.startsWith('+++') ? 'a' : l.startsWith('-') && !l.startsWith('---') ? 'd' : 'h';
    return c === 'h' ? `<span class="h">${h(l)}</span>` : `<span class="${c}"><span class="mk" aria-hidden="true">${l[0]}</span>${h(l.slice(1))}</span>`;
  }).join('\n')}</pre>`;
}
function previewBody(pv, key) {
  if (!pv.cases.length) return `<div class="card-h">${glyph('info', 'Physics model unchanged')}<b>Edit preview</b></div>`;
  const sel = S.open[key + ':case'] || pv.cases[pv.cases.length - 1].case;
  const c = pv.cases.find(x => x.case === sel) || pv.cases[pv.cases.length - 1];
  return `<div class="card-h"><b>Edit preview</b><span class="cases seg-s">${pv.cases.map(x => `<button type="button" class="chip-case ${x.case === c.case ? 'on' : ''}" data-pvcase="${h(key)}:${h(x.case)}" aria-pressed="${x.case === c.case}" title="${h(caseLabel(x.case))}">${h(x.case)}</button>`).join('')}</span></div>
    <ul class="items">${c.model_diff.map(d => `<li><span class="it-k">${h(fieldLabel(d.path) || d.path)}</span><span class="it-v num">${h(showVal(d.a))} → ${h(showVal(d.b))}</span></li>`).join('')}</ul>
    <details data-k="${key}:omc"${opened(key + ':omc') ? ' open' : ''}><summary>${ic('chev', 'tw')}<span>OpenMC model.xml</span></summary>${diffPre(c.files.openmc.slice(2), 'OpenMC input diff')}</details>
    <details data-k="${key}:mc"${opened(key + ':mc') ? ' open' : ''}><summary>${ic('chev', 'tw')}<span>Reference MC input</span></summary>${diffPre(c.files.ref_mc.slice(2), 'Reference MC input diff')}</details>`;
}
/* ---------- Edit Proposals ---------- */
const DONE_P = {applied: ['pass', 'Applied'], dropped: ['dropped', 'Dropped'], expired: ['info', 'Expired'], superseded: ['info', 'Superseded']};
const propOf = id => ((S.snap && S.snap.proposals) || []).find(p => p.id === id);
const unitText = t => String(t).replaceAll('g/cm3', 'g/cm³');
function proposalCard(id) {
  const p = propOf(id);
  if (!p) return '';
  const live = p.status === 'open' && !p.stale;
  const [k, l] = live ? [] : p.status === 'open' ? DONE_P.expired : (DONE_P[p.status] || ['info', p.status]);
  const ch = {};
  p.items.filter(it => it.tool === 'change_params').forEach(it => Object.assign(ch, it.changes));
  let pv = '';
  if (live && Object.keys(ch).length) {
    const x = S.previews[id];
    if (!x) { loadPreview(id, ch); pv = `<div class="card-h">${glyph('srun', 'Generating preview')}<b>Edit preview</b></div>`; }
    else if (x.error) pv = `<div class="card-h">${glyph('fail', 'Preview failed')}<b>Edit preview</b></div><p class="mut">${h(x.error)}</p>`;
    else pv = previewBody(x, `pv:${id}`);
    pv = `<div class="pv-in">${pv}</div>`;
  }
  const gone = !live && !['applied', 'dropped'].includes(p.status);
  return `<div class="card prop ${live ? '' : 'old'}"><div class="card-h">${k ? glyph(k, l) : ''}<b>Proposed edits</b></div>
    <ul class="pitems">${p.items.map(it => `<li>${inlineMd(unitText(it.text))}</li>`).join('')}</ul>${pv}
    ${gone ? '<p class="mut">The goal changed after these edits were proposed; this card is expired. Re-state the edit if still needed.</p>' : ''}
    ${live && !ro() ? `<div class="acts"><button class="btn pri" type="button" data-prop="${h(id)}:apply">Apply</button><button class="btn" type="button" data-prop="${h(id)}:drop">Drop</button></div>` : ''}</div>`;
}
/* ---------- Enter Work Mode Request ---------- */
function workCard(id) {
  const p = propOf(id);
  if (!p) return '';
  const reason = unitText((p.items[0] || {}).reason || '');
  if (p.status === 'open' && goalMode(S.snap) === 'chat') {
    return `<div class="card prop"><div class="card-h">${glyph('wait', 'Awaiting confirmation')}<b>Enter work mode</b></div><p>${h(reason)}</p>
      ${ro() ? '' : `<div class="acts"><button class="btn pri" type="button" data-work-open="${h(id)}">Enter work mode</button><button class="btn" type="button" data-work-drop="${h(id)}">Cancel</button></div>`}</div>`;
  }
  const [k, l] = p.status === 'dropped' ? ['none', 'Stayed in chat mode'] : p.status === 'applied' ? ['pass', 'Entered work mode'] : ['info', 'Superseded by newer request'];
  const item = p.items[0] || {};
  if (p.status === 'applied') {
    const text = item.applied_changes ? 'Applied edits and entered work mode' : item.had_changes && !item.changes_applied
      ? 'Entered work mode without applying message edits' : 'Entered work mode';
    return `<div class="line">${glyph(k, text)}<span>${h(text)}</span></div>`;
  }
  return `<div class="line">${glyph(k, l)}<span>${h(l)}: ${h(reason)}</span></div>`;
}
/* ---------- Trial Run Cards ---------- */
function trialGlyph(p) {
  const t = p && p.trial && p.trial.task;
  if (!t) return '';
  const it = p.items[0] || {}, pr = S.progress[`trial:${p.id}`], n = nodeOf(t.where, pr, t.id);
  let [k, l] = t.status === 'running' && t.remote && !n ? ['none', 'Queued for compute node'] : TASK[t.status] || ['none', t.status];
  let f = 0;
  if (t.status === 'running' && pr && pr.task === t.id && pr.batch) {
    const nb = pr.batches || it.batches;
    f = pr.batch / nb;
    l = `Batch ${pr.batch}/${nb}`;
  }
  return glyph(k, l + where(n), f);
}
function trialCard(id) {
  const p = propOf(id);
  if (!p) return '';
  const sn = S.snap, it = p.items[0] || {}, v = p.trial || {}, t = v.task;
  const live = p.status === 'open' && !p.stale;
  const [dk, dl] = p.status === 'open' ? DONE_P.expired : (DONE_P[p.status] || ['info', p.status]);
  const head = t ? trialGlyph(p) : !live ? glyph(dk, dl)
    : v.cached ? glyph('done-na', 'Cached result matches this input; clicking Run reuses it at zero budget cost') : '';
  const left = sn.budget.left_s, need = (it.estimate || {}).wall_s || 0;
  const fits = v.cached || need <= left;
  const li = (k, val, cls = '') => `<li><span class="it-k">${h(k)}</span><span class="it-v ${cls}">${val}</span></li>`;
  const rows = (it.rows || []).length
    ? it.rows.map(r => li(r.unit && r.unit !== '1' ? `${r.label} (${unit(r.unit)})` : r.label, `${h(showVal(r.old))} → ${h(showVal(r.new))}`, 'num'))
    : [li('Physics model', `Current model for ${h(it.case)}`)];
  rows.push(li('Preset', h(presetText(it.preset))));
  if ((!t || t.status !== 'done') && !v.cached) {
    rows.push(li('Estimated time', dur(need)));
    if (live) rows.push(li('Budget remaining', `${fits ? '' : glyph('warn', 'Exceeds remaining budget')}<span>${dur(Math.max(0, left))}</span>`));
  }
  (it.hints || []).forEach((x, i) => rows.push(li(i ? '' : 'Note', `${glyph('warn', 'Note')}<span>${h(unitText(x))}</span>`, 'hint')));
  const r = t && t.status === 'done' ? t.result || {} : null;
  if (r && r.k != null) {
    rows.push(li('k', `<span class="num">${fmt(r.k)} ± ${fmt(r.sigma)}</span>`));
    if (r.base) rows.push(li(`Δk vs. ${it.case}`, `<span class="num">${pcm(r.delta_pcm)} ± ${r.sigma_pcm.toFixed(0)} pcm</span>`));
  }
  const gone = !live && p.status === 'open' || p.status === 'expired';
  return `<div class="card prop trial ${live || t ? '' : 'old'}"><div class="card-h">${t ? `<span class="tg" data-tprog="${h(id)}">${head}</span>` : head}<b>Trial run</b><span class="cases">${caseChip(it.case)}</span></div>
    <ul class="items">${rows.join('')}</ul>
    ${gone ? '<p class="mut">The goal changed after this trial was proposed; this card is expired.</p>' : ''}
    ${live && !ro() ? `<div class="acts"><button class="btn pri" type="button" data-prop="${h(id)}:apply" ${fits && goalMode(sn) === 'work' ? '' : 'disabled'}>Run</button><button class="btn" type="button" data-prop="${h(id)}:drop">Drop</button></div>` : ''}</div>`;
}
function sameCard(p) {
  return p && (S.snap.proposals || []).find(x => x.kind === 'edit' && x.chat === p.chat && x.status === 'open' && !x.stale);
}
const cardText = c => c.items.map(it => unitText(it.text)).join('; ');
function precheckFailed() {
  const sn = S.snap;
  return sn.goal.status === 'ready' && sn.precheck && sn.precheck.status === 'fail';
}
function sameGone(p) {
  return p && (S.snap.proposals || []).find(x => x.kind === 'edit' && x.chat === p.chat && (x.status !== 'open' || x.stale));
}
function workFacts(same, gone) {
  const sn = S.snap, g = sn.goal, det = sn.precheck && sn.precheck.detail;
  const rows = [], tag = same ? ' (excluding message edits)' : '';
  if (same) rows.push(['Message edits', cardText(same)]);
  else if (gone) rows.push(['Message edits', `${cardText(gone)}. ${gone.status === 'applied' ? 'Already applied' : 'Expired; re-state if needed'}`]);
  if (g.status === 'draft' || !det || !det.complete) rows.push(['Start', 'Starts under selected policy once prechecks finish']);
  else if (g.status !== 'ready') rows.push(['Start', g.paused ? 'Resumes runs' : 'Proceeds under selected policy']);
  else if (precheckFailed()) rows.push(['Precheck failed', (det.blocking || []).join('; ') || sn.precheck.summary || '']);
  else {
    const todo = (det.cases || []).filter(c => c.solver_input && !c.cached).map(c => c.case);
    const lim = sn.budget.limits || {}, used = sn.budget.used || {};
    const need = det.estimate ? det.estimate.wall_s + (det.estimate.ref_mc_s || 0) : 0;
    rows.push(['Runs', (todo.length ? todo.join(', ') : 'All cases cached; no solver runs required') + (det.estimate && det.estimate.ref_mc_s ? `; Reference MC comparison on ${sn.goal.spec.acceptance.case}` : '') + tag]);
    if (need) rows.push(['Estimated time', dur(need) + tag]);
    if (lim.wall_s) rows.push(['Budget remaining', dur(Math.max(0, lim.wall_s - (used.wall_s || 0)))]);
  }
  const other = (sn.proposals || []).filter(x => x.kind === 'edit' && x.status === 'open' && !x.stale && x !== same);
  if (other.length) rows.push(['Unapplied edits', other.map(cardText).join('; ') + '. Excluded from this run']);
  if (same) rows.push(['', 'Click "Apply edits & start work" to apply these edits, re-run prechecks, and start; click "Enter work mode" to run with the current goal as-is']);
  return rows;
}
function renderWorkDlg() {
  const w = S.workDlg, p = propOf(w.id), d = $('#workDlg');
  if (!p || p.status !== 'open') { if (d.open) d.close(); S.workDlg = null; return; }
  const same = sameCard(p), gone = same ? null : sameGone(p), fail = precheckFailed(), v = S.snap.goal.version;
  const reason = unitText((p.items[0] || {}).reason || '');
  w.edit = same ? same.id : null;
  const html = `<form class="wd" method="dialog">
    <h2 id="workT">Enter work mode</h2>${reason ? `<p>${h(reason)}</p>` : ''}
    ${v !== w.opened ? `<p class="wd-note">Goal updated while dialog was open; showing current state.</p>` : ''}
    <dl>${workFacts(same, gone).map(([k, x]) => `<dt>${h(k)}</dt><dd>${h(x)}</dd>`).join('')}</dl>
    <div class="wd-pol"><span class="mut" id="workPolT">Approval policy</span><div class="modes sm" role="group" aria-labelledby="workPolT">${[['ask', 'Approve each'], ['goal', 'Auto']].map(([k, l]) =>
      `<button type="button" aria-pressed="${w.policy === k}" data-work-pol="${k}">${l}</button>`).join('')}</div></div>
    <div class="acts"><button class="btn" type="button" data-work-cancel="1">Cancel</button><button class="btn ${same ? '' : 'pri'}" type="button" data-work-go="1" ${fail ? 'disabled' : ''}>Enter work mode</button>${
      same ? `<button class="btn pri" type="button" data-work-go="edit">Apply edits & start work</button>` : ''}</div></form>`;
  w.version = v;
  if (d.dataset.h === html) return;
  const held = document.activeElement && d.contains(document.activeElement) ? document.activeElement : null;
  const key = held && ['workPol', 'workGo', 'workCancel'].map(k => held.dataset[k] && `[data-${k.replace(/[A-Z]/g, c => '-' + c.toLowerCase())}="${held.dataset[k]}"]`).find(Boolean);
  d.innerHTML = html;
  d.dataset.h = html;
  const back = key && $(key, d);
  if (back && !back.disabled) back.focus();
  else if (held) $('[data-work-cancel]', d).focus();
}
function workFocus() {
  const d = $('#workDlg');
  const b = $('[data-work-go="edit"]', d) || $('[data-work-go="1"]:not(:disabled)', d) || $('[data-work-cancel]', d);
  b.focus();
}
function openWorkDialog(id) {
  S.workDlg = {id, policy: S.snap.goal.mode === 'ask' ? 'ask' : 'goal', opened: S.snap.goal.version};
  renderWorkDlg();
  const d = $('#workDlg');
  if (!S.workDlg) return;
  if (!d.open) d.showModal();
  workFocus();
}
function maybeWorkDialog() {
  const sn = S.snap;
  if (S.workDlg && $('#workDlg').open) { renderWorkDlg(); return; }
  if (!sn || ro() || goalMode(sn) !== 'chat' || $('#workDlg').open || $('#llmDlg').open) return;
  const p = (sn.proposals || []).find(x => x.kind === 'work' && x.status === 'open');
  if (!p || S.workSeen[p.id]) return;
  S.workSeen[p.id] = true;
  openWorkDialog(p.id);
}
function readingCard(r, rows, i) {
  const p = meta(r).parse;
  if (p.param == null || p.alternative == null) return '';
  const tried = (S.snap.goal.spec.tried_readings || []).includes(`${p.param}#${p.alternative}`);
  if (tried || laterEdit(rows, i) || ro()) return '';
  return `<div class="acts"><button class="btn pri" data-reading-apply="${h(p.param)}:${h(p.alternative)}" type="button">Apply</button></div>`;
}

/* ---------- Parameter List Card ---------- */
const NAT = {given: ['given', 'Given in source'], derived: ['derived', 'Derived'], assumed: ['assumed', 'Assumed'], edited: ['edited', 'User edited'],
  missing: ['missing', 'Missing'], conflict: ['conflict', 'Two routes differ'], ambiguous: ['conflict', 'Two readings'], candidate: ['candidate', 'Figure candidate, verify'], user: ['edited', 'User entered']};
function natMark(nature) {
  const [k, l] = NAT[nature] || ['given', nature];
  return `<span class="nat nat-${k}" role="img" aria-label="${h(l)}" title="${h(l)}"></span>`;
}
function slotValue(p) {
  if (p.value == null) return '—';
  if (p.id === 'layout') return `${p.value.length} × ${p.value.length}`;
  if (p.kind === 'figure') return `${p.value.length} rows`;
  if (p.kind === 'mixture') return `${Object.keys(p.value).length} nuclides`;
  return typeof p.value === 'number' ? +p.value.toPrecision(7) : showVal(p.value);
}
function pageBtn(p, alt, id, fk = 'src') {
  const page = alt !== undefined ? (p.alternatives[alt] || {}).page : p.page;
  const key = `${id || p.id}${alt !== undefined ? ':' + alt : ''}`;
  return page ? `<button class="lnk" type="button" data-src="${h(key)}" data-fk="${fk}:${h(key)}" title="View source">p. ${h(page)}</button>` : '';
}
function slotRowHtml(p) {
  if (p.struck) return `<tr><td>${natMark(p.struck.nature)}</td><td><s>${h(p.label)}</s></td><td class="sl-v"><s><span class="num">${h(slotValue(p.struck))}</span> <span class="u">${h(unit(p.struck.unit))}</span></s></td><td></td></tr>`;
  const val = `<span class="num">${h(slotValue(p))}</span> <span class="u">${h(unit(p.unit))}</span>`;
  return `<tr title="${h(p.note || '')}"><td>${natMark(p.nature)}</td><td>${h(p.label)}${p.nature === 'assumed' ? `<span class="sl-r">${h(p.reading)}</span>` : ''}${p.nature === 'ambiguous' ? `<span class="sl-r">${h(p.alternatives.map(a => a.reading).join(' or '))}</span>` : ''}${(p.value == null || p.nature === 'candidate' || (p.flags || []).some(f => ['unit', 'rejected', 'nosigma', 'single'].includes(f))) && p.note ? `<span class="sl-r">${h(p.source ? `${p.source}: ${p.note}` : p.note)}</span>` : ''}</td><td class="sl-v">${val}</td><td>${pageBtn(p)}</td></tr>`;
}
function figureRows(p) {
  if (p.kind !== 'figure' || !p.value) return '';
  const k = `xfig:${p.id}`, legend = Object.entries(p.legend || {}).map(([c, t]) => `${c} ${t}`).join(', ');
  return `<details data-k="${k}"${opened(k) ? ' open' : ''}><summary>Extracted ${h(p.label)}</summary><pre class="figrows num">${h(p.value.join('\n'))}</pre>${legend ? `<p class="mut">${h(legend)}</p>` : ''}</details>`;
}
function layoutSvg(p, edit) {
  const rows = p.value, n = rows.length, s = 16, W = n * s + 2;
  const was = p.original && p.original.value;
  const cell = {G: 'var(--m-water)', I: 'var(--accent)', F: 'var(--m-fuel)'};
  const count = c => rows.reduce((a, r) => a + r.split(c).length - 1, 0);
  const name = {F: 'Fuel pin', G: 'Guide tube', I: 'Instrument tube'};
  const dots = rows.map((row, r) => row.split('').map((ch, c) => {
    const cx = 1 + c * s + s / 2, cy = 1 + r * s + s / 2;
    return `<circle cx="${cx}" cy="${cy}" r="${s * 0.4}" fill="${cell[ch]}" class="${ch === 'F' ? 'lp' : 'lp lt'}${was && was[r] && was[r][c] !== ch ? ' lchg' : ''}"${edit ? ` data-cell="${r}:${c}" data-fk="cell:${r}:${c}" tabindex="0" role="button" aria-label="Row ${r + 1}, col ${c + 1}: ${name[ch]}"` : ''}><title>Row ${r + 1}, col ${c + 1}: ${name[ch]}</title></circle>`;
  }).join('')).join('');
  return `<div class="lay"><svg class="laysvg" viewBox="0 0 ${W} ${W}" role="img" aria-label="Pin layout ${n} × ${n}">${dots}</svg>
    <ul class="layk">${['F', 'G', 'I'].map(c => `<li><i style="background:${cell[c]}"></i>${name[c]}<span class="num">${count(c)}</span></li>`).join('')}</ul></div>`;
}
function checksList(ex) {
  const cs = ex.checks || [];
  if (!cs.length) return '';
  const k = `xchecks:${S.snap.goal.id}`, bad = cs.filter(c => c.status !== 'pass');
  return `<details data-k="${k}"${opened(k) || bad.length ? ' open' : ''}><summary>${glyph(bad.some(c => c.status === 'fail') ? 'fail' : bad.length ? 'warn' : 'pass', 'Consistency checks')}<span>Consistency checks</span><span class="count num">${cs.length - bad.length}/${cs.length}</span></summary>
    <ul class="xcheck">${cs.map(c => `<li>${glyph(c.status, c.status)}<span>${h(c.text)}</span></li>`).join('')}</ul></details>`;
}
function extractCard(r) {
  const sn = S.snap, ex = sn.extraction;
  if (!ex) return paramsCard(r);
  let n = S.nodes[sn.goal.spec.paramlist];
  if (!n) {
    node(sn.goal.spec.paramlist).then(renderThread).catch(() => {});
    if (!S.xlast || S.xlast.goal !== sn.goal.id) return '';
    n = S.xlast.node;
  }
  S.xlast = {goal: sn.goal.id, node: n};
  const pl = n.content;
  if (['pending', 'running'].includes(ex.state)) return `<div class="card"><div class="card-h">${glyph('prep', 'Reading documents')}<b>Parameters</b></div></div>`;
  if (ex.state === 'failed') {
    return `<div class="card"><div class="card-h">${glyph('bad', 'Extraction failed')}<b>Parameters</b></div>
      ${ro() ? '' : `<div class="acts"><button class="btn pri" type="button" data-act="extract-retry">Re-extract</button>${rowsOf(pl).some(p => p.value != null) ? '<button class="btn ghost" type="button" data-panel="params">View list</button>' : ''}</div>`}</div>`;
  }
  if (ex.review) {
    const rv = ex.review, done = rv.ticks.length, ready = !ex.blocking.length && done === rv.need.length;
    return `<div class="card">
      <div class="card-h">${glyph(ready ? 'pass' : 'wait', ready ? 'Ready to confirm' : 'Pending review')}<b>Parameters</b><span class="count num">${done}/${rv.need.length}</span></div>
      ${ex.blocking.length ? `<ul class="xnotes">${ex.blocking.map(x => `<li>${h(x)}</li>`).join('')}</ul>` : ''}
      ${ro() ? '' : `<div class="acts"><button class="btn pri" type="button" data-panel="params">Review items</button><button class="btn ghost" type="button" data-act="extract-retry">Re-extract</button></div>`}</div>`;
  }
  const figures = pl.params.filter(p => p.kind === 'figure' && p.value);
  const covered = new Set((pl.checks || []).filter(c => c.status === 'pass').flatMap(c => c.independent || []));
  const unchecked = p => (p.flags || []).includes('single') && !covered.has(p.id);
  const needs = p => p.struck || p.kind === 'added' || (p.value == null && !p.optional) || ['conflict', 'candidate', 'assumed', 'ambiguous'].includes(p.nature) || (p.flags || []).some(f => ['basis', 'unit', 'nosigma', 'rejected'].includes(f)) || unchecked(p) || (p.picked_from || p.nature === 'user');
  const all = rowsOf(pl);
  const top = all.filter(needs).map(p => unchecked(p) && !p.note ? {...p, note: 'Single-route only'} : p), rest = all.filter(p => !needs(p));
  const lay = pl.params.find(p => p.id === 'layout' && p.value);
  const k = `xrest:${r.id}`;
  const notes = (ex.blocking || []).filter(b => !/^(Missing|Conflict):/i.test(b) && !ex.list_only);
  const table = rows => `<div class="tblw"><table class="tbl slots"><tbody>${rows.map(p => slotRowHtml(p)).join('')}</tbody></table></div>`;
  return `<div class="card">
    <div class="card-h">${ex.list_only ? glyph('done-na', 'Extracted list') : glyph(ex.state === 'confirmed' ? 'done' : ex.blocking.length ? 'wait' : 'pass', ex.state === 'confirmed' ? 'Confirmed' : ex.blocking.length ? 'Action needed' : 'Ready')}<b>Parameters</b><span class="count num">${all.length}</span>
      <button class="ib sm push" type="button" data-panel="params" aria-label="Open in workspace" title="Open in workspace">${ic('open')}</button></div>
    ${notes.length ? `<ul class="xnotes">${notes.map(x => `<li>${h(x)}</li>`).join('')}</ul>` : ''}
    ${top.length ? table(top) : ''}
    ${lay ? layoutSvg(lay, false) : ''}
    ${figures.map(figureRows).join('')}
    <details data-k="${k}"${opened(k) ? ' open' : ''}><summary>${top.length ? 'Other' : 'All'} ${rest.length} parameters</summary>${table(rest)}</details>
    ${checksList(ex)}
    </div>`;
}
function paramsCard(r) {
  const n = S.nodes[S.snap.goal.spec.paramlist];
  if (!n) { node(S.snap.goal.spec.paramlist).then(renderThread).catch(() => {}); }
  const pl = n && n.content;
  const k = `params:${r.id}`;
  const mods = {}; (S.snap.modifications || []).forEach(m => { mods[m.param] = m; });
  return `<div class="card">
    <details data-k="${k}"${opened(k) ? ' open' : ''}><summary class="card-h">${ic('table', 'cic')}<b>Parameters</b><span class="count num">${pl ? pl.params.length : ''}</span>
      <button class="ib sm push" type="button" data-panel="params" aria-label="Open in workspace" title="Open in workspace">${ic('open')}</button></summary>
      ${pl ? `<div class="tblw"><table class="tbl"><thead><tr><th>Parameter</th><th>Value</th><th>Unit</th><th><span class="sr">Source</span></th></tr></thead><tbody>
        ${pl.params.map(p => `<tr><td>${h(p.label)}</td><td class="num">${typeof p.value === 'number' ? h(+p.value.toPrecision(7)) : h(showVal(p.value))}</td>
          <td>${h(unit(p.unit))}</td><td>${natMark(mods[p.id] ? 'edited' : p.nature)}</td></tr>`).join('')}</tbody></table></div>` : ''}
    </details></div>`;
}

/* ---------- Precheck Card ---------- */
function checksCard(run) {
  let ck = (S.snap.checks || {})[run];
  if (!ck) return '';
  const steps = ck.steps || [];
  if (ck.status === 'done' && ck.version != null && ck.version < S.snap.goal.version) ck = Object.assign({}, ck, {status: 'superseded'});
  const pc = S.snap.precheck;
  const cur = pc && pc.detail && pc.detail.checks === ck.id;
  const worst = cur && pc.status !== 'running' ? pc.status : ['fail', 'warn', 'pass'].find(s => steps.some(x => x.status === s)) || 'info';
  const head = ck.status === 'running' ? glyph('srun', 'Prechecks running') : ck.status === 'interrupted' ? glyph('stopped', 'Prechecks interrupted')
    : ck.status === 'error' ? glyph('bad', 'Precheck error') : ck.status === 'superseded' ? glyph('none', 'Superseded by newer precheck') : vglyph(worst);
  const rowsH = steps.map(st => {
    const key = `ck:${ck.id}:${st.n}`;
    const fresh = S.seenReady && !S.seenSteps.has(key);
    S.seenSteps.add(key);
    const c = st.args && st.args.case;
    const dd = st.detail && typeof st.detail === 'object' && 'data' in st.detail ? st.detail.data : st.detail;
    const det = dd != null ? (typeof dd === 'string' ? dd : JSON.stringify(dd, null, 1)) : '';
    const dsum = st.detail && st.detail.summary && st.detail.summary !== st.finding ? st.detail.summary : '';
    const bthen = dd && typeof dd === 'object' && dd.budget && dd.budget.wall_s, bnow = S.snap.budget && S.snap.budget.limits.wall_s;
    const bnote = bthen && bnow && bthen !== bnow ? ` (wall budget updated from ${dur(bthen)} to ${dur(bnow)})` : '';
    return `<li class="step ${fresh ? 'fresh' : ''}">
      <details data-k="${key}"${opened(key) ? ' open' : ''}><summary>${st.status === 'running' && ck.status !== 'running' ? glyph('none', 'Incomplete') : vglyph(st.status)}<span class="st-t">${h(st.title)}</span>${c ? caseChip(c) : ''}
        ${st.finding ? `<span class="st-f">${h(st.finding)}${bnote}</span>` : ''}</summary>
        <div class="st-d">${thinkBlock([st.call], `${ck.id}:${st.n}`)}${dsum ? `<p>${h(dsum)}</p>` : ''}${det ? `<pre class="logt">${h(det.length > 20000 ? det.slice(0, 20000) + '\n...' : det)}</pre>` : ''}
          ${st.evidence ? `<button class="btn sm ghost" type="button" data-panel="evidence" data-ev="${h(st.evidence)}">Evidence</button>` : ''}</div></details></li>`;
  }).join('');
  return `<div class="card ${ck.status === 'superseded' ? 'old' : ''}"><div class="card-h">${head}<b>Prechecks</b><span class="count num">${steps.length}</span></div>
    <ol class="steps">${rowsH}</ol>${ck.status === 'running' ? liveBlock(['precheck']) : ''}</div>`;
}

/* ---------- Confirmation Card ---------- */
function confirmCard() {
  const sn = S.snap, pc = sn.precheck;
  const cached = {}; ((pc && pc.detail && pc.detail.cases) || []).forEach(c => { cached[c.case] = c.cached; });
  const ok = !(pc && pc.status === 'fail');
  return `<div class="card confirm"><div class="card-h"><b>Target cases</b></div>
    <div class="sq">${sn.cases.map(c => `<span class="sqi ${cached[c.case] ? 'cached' : ''}" title="${h(c.case)} ${h(c.label)}${cached[c.case] ? ' (cached)' : ''}">${h(c.case)}</span>`).join('')}</div>
    ${ro() ? '' : `<div class="acts"><button class="btn pri" data-act="start-work" type="button" ${ok ? '' : 'disabled'}>Start work</button><button class="mpick" data-act="ppick" type="button" aria-haspopup="true" aria-label="${policyLabel()}">${policyHtml()}</button></div>`}</div>`;
}

/* ---------- Run Progress Card ---------- */
function totalBatches(cid) {
  const p = S.progress[cid];
  if (p && p.batches) return p.batches;
  const spec = S.snap.goal.spec;
  return (presetRun((spec.case_presets || {})[cid] || spec.run_preset) || PRESETS.verify).batches;
}
const TASK = {queued: ['none', 'Queued'], running: ['run', 'Running'], stopping: ['run', 'Stopping'], stopped: ['stopped', 'Stopped'],
  stop_unconfirmed: ['bad', 'Stop unconfirmed'], interrupted: ['stopped', 'Interrupted'], failed: ['bad', 'Failed'], superseded: ['info', 'Superseded'],
  cancelled: ['none', 'Cancelled'], done: ['done', 'Done']};
function nodeOf(where, p, tid) { return where || (p && p.task === tid && p.node) || null; }
function where(n) { return n ? `, ${n}` : ''; }
function progCell(cid) {
  const c = S.snap.cases.find(x => x.case === cid) || {};
  const t = c.task, p = S.progress[cid], n = t ? nodeOf(t.where, p, t.id) : null;
  let f = 0, gk = 'none', gl = 'Not started';
  if (c.result) { f = 1; gk = 'done'; gl = c.result.trial ? 'Reused trial result' : t ? 'Done' : 'Cached'; if (t && c.result.task === t.id) gl += where(n); }
  else if (t) {
    [gk, gl] = t.status === 'running' && t.remote && !n ? ['none', 'Queued for compute node'] : TASK[t.status] || ['none', t.status];
    if (p && p.batch && p.task === t.id) f = p.batch / totalBatches(cid);
    if (t.status === 'running' && p && p.batch && p.task === t.id) gl = `Batch ${p.batch}/${totalBatches(cid)}`;
    gl += where(n);
  }
  const k = c.result ? `<span class="num kv">${fmt(c.result.k)} ± ${fmt(c.result.sigma)}</span>` : '';
  return `<span class="bar ${c.result ? 'full' : ''}" title="${h(gl)}"><b style="width:${(100 * Math.min(1, f)).toFixed(1)}%"></b></span><span class="rk">${k || glyph(gk, gl, f)}</span>`;
}
function refMcCell(cid) {
  const c = S.snap.cases.find(x => x.case === cid) || {}, m = c.ref_mc;
  if (!m) return '';
  const p = S.progress[`refmc:${cid}`], n = nodeOf(m.where, p, m.task);
  let [gk, gl] = m.k != null ? ['done', 'Done'] : m.status === 'running' && m.remote && !n ? ['none', 'Queued for compute node'] : TASK[m.status] || ['none', m.status];
  let f = 0;
  if (gk === 'run') {
    if (p && p.task === m.task && p.cycle && p.cycles) { f = p.cycle / p.cycles; gl = `Cycle ${p.cycle}/${p.cycles}`; }
    else gk = 'srun';
  }
  gl += where(n);
  return `${m.k != null ? `<span class="num kv">${fmt(m.k)} ± ${fmt(m.sigma)}</span>` : ''}${glyph(gk, gl, f)}`;
}
function refMcRow(c) {
  if (!c.ref_mc) return '';
  return `<li class="rmc"><button class="rc num" type="button" data-panel="input" data-case="${h(c.case)}" data-solver="ref_mc">${h(c.case)}</button><span class="rl">Reference MC</span>
    <span class="rp" data-rprog="${h(c.case)}">${refMcCell(c.case)}</span></li>`;
}
function runsCard() {
  const sn = S.snap, st = goalState(sn);
  const done = sn.cases.filter(c => c.result).length;
  return `<div class="card"><div class="card-h">${glyph(done === sn.cases.length ? 'done' : st.k === 'paused' || st.k === 'stopped' ? st.k : 'run', st.l, done / sn.cases.length)}<b>Runs</b><span class="count num">${done}/${sn.cases.length}</span></div>
    <ul class="runs">${sn.cases.map(c => `<li><button class="rc num" type="button" data-panel="input" data-case="${h(c.case)}">${h(c.case)}</button><span class="rl">${h(c.label)}</span>
      <span class="rp" data-prog="${h(c.case)}">${progCell(c.case)}</span></li>` + refMcRow(c)).join('')}</ul></div>`;
}

/* ---------- Results Card ---------- */
function resultsCard() {
  const sn = S.snap, acc = sn.goal.spec.acceptance, cs = sn.cases;
  let prev = null;
  const rows = cs.map(c => {
    const r = c.result;
    let dk = '—';
    if (r && prev) dk = `${pcm((r.k - prev.k) * 1e5)} ± ${(Math.hypot(r.sigma, prev.sigma) * 1e5).toFixed(0)}`;
    prev = r || null;
    return `<tr><td class="num">${h(c.case)}</td><td>${h(c.label)}</td><td class="num">${r ? `${fmt(r.k)} ± ${fmt(r.sigma)}` : '—'}</td>
      <td class="num">${dk}</td><td class="num">${r ? dur(r.wall_s) : '—'}</td></tr>`;
  }).join('');
  const mc = cs.filter(c => c.ref_mc && c.ref_mc.k != null).map(c => `<tr title="Δk vs. OpenMC on same case"><td class="num">${h(c.case)}</td><td>Reference MC (Δk vs. OpenMC)</td><td class="num">${fmt(c.ref_mc.k)} ± ${fmt(c.ref_mc.sigma)}</td>
      <td class="num">${c.result ? `${pcm((c.ref_mc.k - c.result.k) * 1e5)} ± ${(Math.hypot(c.ref_mc.sigma, c.result.sigma) * 1e5).toFixed(0)}` : '—'}</td><td class="num">${dur(c.ref_mc.wall_s)}</td></tr>`).join('');
  return `<div class="card"><div class="card-h"><b>Results</b></div>
    <div class="tblw"><table class="tbl nw"><thead><tr><th>Case</th><th>Step</th><th>k ± σ</th><th title="OpenMC rows vs. prior case; Reference MC rows vs. same-case OpenMC">Δk (pcm)</th><th>Time</th></tr></thead><tbody>${rows}${mc}</tbody></table></div>
    ${chart(cs, acc)}</div>`;
}
function cardW(def) {
  const th = $('#thread');
  const w = th && th.clientWidth ? th.clientWidth - 48 - 34 : def;
  return Math.round(Math.max(300, Math.min(def, w)));
}
function chart(cs, acc) {
  const pts = cs.map((c, i) => ({i, c: c.case, r: c.result})).filter(p => p.r);
  if (!pts.length || !acc.reference || cs.length < 2) return '';
  const W = cardW(690), H = W < 420 ? 210 : 250, L = 50, R = 10, T = 14, B = 28;
  const ref = acc.reference.value;
  const s5 = (cs.find(c => c.case === acc.case) || {}).result;
  const tol = s5 ? acc.k_sigma * Math.hypot(s5.sigma, acc.reference.sigma) : null;
  let lo = ref, hi = ref;
  pts.forEach(p => { lo = Math.min(lo, p.r.k - 2 * p.r.sigma); hi = Math.max(hi, p.r.k + 2 * p.r.sigma); });
  if (tol) { lo = Math.min(lo, ref - tol); hi = Math.max(hi, ref + tol); }
  const pad = (hi - lo) * 0.08 || 0.01; lo -= pad; hi += pad;
  const x = i => L + (W - L - R) * (i + 0.5) / cs.length;
  const y = k => T + (H - T - B) * (hi - k) / (hi - lo);
  const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / 4)));
  const st = [1, 2, 5].map(m => m * step).find(s => (hi - lo) / s <= 6) || step * 10;
  const ticks = [];
  for (let v = Math.ceil(lo / st) * st; v <= hi; v += st) ticks.push(v);
  const dec = Math.max(0, -Math.floor(Math.log10(st)));
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="k ± 1σ across cases with reference and ${h(acc.case)} tolerance band">
    ${ticks.map(v => `<line class="ax" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(dec)}</text>`).join('')}
    ${tol ? `<rect class="refband" x="${L}" width="${W - L - R}" y="${y(ref + tol)}" height="${y(ref - tol) - y(ref + tol)}"/>` : ''}
    <line class="refline" x1="${L}" x2="${W - R}" y1="${y(ref)}" y2="${y(ref)}"/>
    <text x="${L + 6}" y="${y(ref) - 5}" text-anchor="start">Ref ${ref.toFixed(5)}${tol ? `, tol ±${(tol * 1e5).toFixed(0)} pcm` : ''}</text>
    ${cs.map((c, i) => `<text x="${x(i)}" y="${H - 10}" text-anchor="middle">${h(c.case)}</text>`).join('')}
    ${pts.map(p => `<line class="eb" x1="${x(p.i)}" x2="${x(p.i)}" y1="${y(p.r.k + p.r.sigma)}" y2="${y(p.r.k - p.r.sigma)}"/>
      <line class="eb" x1="${x(p.i) - 5}" x2="${x(p.i) + 5}" y1="${y(p.r.k + p.r.sigma)}" y2="${y(p.r.k + p.r.sigma)}"/>
      <line class="eb" x1="${x(p.i) - 5}" x2="${x(p.i) + 5}" y1="${y(p.r.k - p.r.sigma)}" y2="${y(p.r.k - p.r.sigma)}"/>
      <circle class="pt" cx="${x(p.i)}" cy="${y(p.r.k)}" r="4"/>
      <rect class="hit" x="${x(p.i) - 18}" y="${T}" width="36" height="${H - T - B}" data-panel="model" data-case="${h(p.c)}"><title>${h(p.c)} k = ${fmt(p.r.k)} ± ${fmt(p.r.sigma)}</title></rect>`).join('')}
  </svg>`;
}

/* ---------- Acceptance Card ---------- */
function acceptanceCard() {
  const sn = S.snap, a = sn.acceptance, e = a.detail.evaluation, inv = a.detail.investigation;
  const na = e.outcome === 'not_applicable';
  if (e.k_ref == null) return `<div class="card"><div class="card-h">${glyph('done-na', 'Acceptance N/A')}<b>Acceptance</b><span class="mut">${h(sn.goal.spec.acceptance.case)}</span></div>
    <dl class="nums"><dt>k</dt><dd class="num">${fmt(e.k)} ± ${fmt(e.sigma)}</dd><dt>Status</dt><dd>No reference solution in source; skipped</dd></dl></div>`;
  const head = na ? glyph('done-na', 'Acceptance N/A') : e.outcome === 'match' ? glyph('done', 'Match') : glyph('bad', 'Out of tolerance');
  const tol = e.k_sigma * e.sigma_c_pcm, d = e.delta_pcm, sc = e.sigma_c_pcm;
  const span = Math.max(tol * 1.45, Math.abs(d) + sc * 1.6);
  const W = Math.min(440, cardW(440)), L = 18, R = 18, X = v => L + (W - L - R) * (v + span) / (2 * span), Y = 30;
  const inside = Math.abs(d) <= tol;
  const tick = (v, txt) => `<line class="tk" x1="${X(v)}" x2="${X(v)}" y1="${Y + 14}" y2="${Y + 19}"/><text x="${X(v)}" y="${Y + 32}" text-anchor="middle">${txt}</text>`;
  const gauge = `<svg class="gauge" viewBox="0 0 ${W} 70" role="img" aria-label="Δ = ${pcm(d)} pcm, ±σ_c = ${sc.toFixed(0)} pcm, tolerance ±${e.k_sigma}σ_c = ±${tol.toFixed(0)} pcm">
    <line class="axis" x1="${L}" x2="${W - R}" y1="${Y + 14}" y2="${Y + 14}"/>
    <rect class="band" x="${X(-tol)}" y="${Y - 12}" width="${X(tol) - X(-tol)}" height="26" rx="3"/>
    <line class="zero" x1="${X(0)}" x2="${X(0)}" y1="${Y - 14}" y2="${Y + 14}"/>
    ${tick(-tol, pcm(-tol))}${tick(0, '0')}${tick(tol, pcm(tol))}
    <g class="mk ${na ? 'na' : inside ? 'in' : 'out'}"><line x1="${X(Math.max(-span, d - sc))}" x2="${X(Math.min(span, d + sc))}" y1="${Y}" y2="${Y}"/>
      <line x1="${X(Math.max(-span, d - sc))}" x2="${X(Math.max(-span, d - sc))}" y1="${Y - 5}" y2="${Y + 5}"/><line x1="${X(Math.min(span, d + sc))}" x2="${X(Math.min(span, d + sc))}" y1="${Y - 5}" y2="${Y + 5}"/>
      <circle cx="${X(Math.max(-span, Math.min(span, d)))}" cy="${Y}" r="5"/></g>
    <text class="u" x="${W - R}" y="${Y + 32}" text-anchor="end">pcm</text></svg>`;
  return `<div class="card"><div class="card-h">${head}<b>Acceptance</b><span class="mut">${h(sn.goal.spec.acceptance.case)}</span>
      <button class="ib sm push" type="button" data-panel="evidence" data-ev="${a.id}" aria-label="View evidence" title="View evidence">${ic('open')}</button></div>
    <div class="acc">${gauge}
      <dl class="nums"><dt>k</dt><dd class="num">${fmt(e.k)} ± ${fmt(e.sigma)}</dd>
        <dt>Reference</dt><dd class="num">${fmt(e.k_ref, 6)} ± ${fmt(e.sigma_ref, 6)}</dd>
        <dt>Δ</dt><dd class="num">${pcm(d)} ± ${sc.toFixed(0)} pcm</dd></dl></div>
    ${inv ? `<div class="tblw"><table class="tbl"><thead><tr><th>Parameter</th><th>Alternative reading</th><th>Δk (pcm)</th></tr></thead><tbody>
      ${[...inv.candidates, ...(inv.excluded || [])].map(c => `<tr><td>${h(c.label)}</td><td>${h(c.reading)}${(inv.excluded || []).includes(c) ? '<span class="mut"> (excluded in source)</span>' : ''}</td><td class="num">${c.known ? pcm(c.known.dk_pcm) + ' ± ' + c.known.sigma_pcm.toFixed(0) : '—'}</td></tr>`).join('')}
      </tbody></table></div>` : ''}</div>`;
}

/* ---------- Report Card ---------- */
function reportCard(hash) {
  return `<button class="card filecard" type="button" data-panel="report" data-report="${h(hash || '')}">${ic('doc', 'cic')}<b>Report</b>${ic('chev', 'push')}</button>`;
}
