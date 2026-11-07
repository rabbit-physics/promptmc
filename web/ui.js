'use strict';
/* Shared state, RPC/API helpers, escaping & formatting, status glyphs, icons, and Markdown rendering */

const S = {
  goals: [], talks: [], gid: null, tid: null, snap: null, nodes: {}, health: null,
  panel: null, mcase: 'S5', mfield: null, msolver: 'openmc', plotOn: false, plots: {}, preview: null,
  evidence: [], evSel: null, evDetail: null, reportHash: null,
  progress: {}, previews: {}, temp: null, pending: null,
  newMode: 'chat', newPolicy: 'goal', newPreset: null,
  newLlm: null, llmWant: null, live: {}, thinkText: {}, showThink: true, dlg: null, workSeen: {}, workDlg: null,
  open: {}, dvals: {}, seenSteps: new Set(), seenReady: false, pendingReading: {},
  search: '', searchOn: false, showArchived: false, sideOff: false, atts: [], src: null, srcZoom: 1, rv: null,
};
const PANELS = [['params', 'Params'], ['source', 'Source'], ['model', 'Model'], ['input', 'Input'], ['evidence', 'Evidence'], ['report', 'Report']];
/* Particle presets: named tiers sorted by particle count, drill last; custom formatted as <particles>x<batches> */
const PRESETS = {
  smoke: {particles: 1000, batches: 12, inactive: 4},
  quick: {particles: 10000, batches: 60, inactive: 20},
  verify: {particles: 60000, batches: 130, inactive: 30},
  precise: {particles: 120000, batches: 230, inactive: 30},
  drill: {particles: 20000, batches: 100, inactive: 20},
};
const CUSTOM_RANGE = {particles: [100, 1000000], batches: [12, 2000]};
const customInactive = b => Math.min(30, Math.max(4, Math.round(b / 3)));
/* Resolve preset name or "<particles>x<batches>" string -> {particles, batches, inactive} or null */
function presetRun(key) {
  if (PRESETS[key]) return PRESETS[key];
  const m = /^(\d+)x(\d+)$/.exec(key || '');
  return m ? {particles: +m[1], batches: +m[2], inactive: customInactive(+m[2])} : null;
}
/* Normalize (particles, batches) to preset key or null if out of bounds */
function presetKey(p, b) {
  const [pl, ph] = CUSTOM_RANGE.particles, [bl, bh] = CUSTOM_RANGE.batches;
  if (!Number.isInteger(p) || !Number.isInteger(b) || p < pl || p > ph || b < bl || b > bh) return null;
  const named = Object.keys(PRESETS).find(k => k !== 'drill' && PRESETS[k].particles === p && PRESETS[k].batches === b && PRESETS[k].inactive === customInactive(b));
  return named || `${p}x${b}`;
}
/* Compact preset label */
function presetLabel(key) {
  if (PRESETS[key]) return key;
  const r = presetRun(key);
  return r ? `${r.particles.toLocaleString('en')} × ${r.batches}` : String(key ?? '');
}
/* Descriptive preset label for cards */
function presetText(key) {
  const r = presetRun(key);
  if (!r) return String(key ?? '');
  return PRESETS[key] ? `${key} (${r.particles} particles/batch × ${r.batches} batches)` : `${r.particles.toLocaleString('en')} particles/batch × ${r.batches} batches`;
}
/* Approximate human-readable runtime */
const approx = s => s < 60 ? '< 1 min' : s < 3570 ? `~${Math.round(s / 60)} min`
  : s < 172800 ? `~${+(s / 3600).toFixed(1)} hr` : `~${+(s / 86400).toFixed(1)} d`;
const LABEL = {fuel: 'Fuel', gap: 'Gap', clad: 'Cladding', moderator: 'Moderator'};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const h = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const unit = u => String(u ?? '').replace('g/cm3', 'g/cm³');
const fmt = (x, d = 5) => (x == null || isNaN(x)) ? '—' : Number(x).toFixed(d);
const pcm = x => (x == null) ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(Math.round(x))}`;
const dur = s => {
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 7200) return `${Math.round(s / 60)} min`;
  const m = Math.round(s / 60), hh = Math.floor(m / 60);
  return `${hh} hr` + (m % 60 ? ` ${m % 60} min` : '');
};
const meta = r => { if (!r || !r.meta) return {}; if (typeof r.meta === 'object') return r.meta; try { return JSON.parse(r.meta) || {}; } catch (_) { return {}; } };
const ro = () => !!(S.snap ? S.snap.goal.archived : (S.talks.find(t => t.id === S.tid) || {}).archived);
/* Source tab is shown only when the goal has uploaded documents */
const panelsNow = () => PANELS.filter(([k]) => k !== 'source' || (S.snap && (S.snap.docs || []).length));

async function api(path, opt) {
  const r = await fetch(path, opt);
  if (!r.ok) {
    let msg = `${r.status} ${path}`;
    try { const j = await r.json(); if (j && (j.error || j.message)) msg = (j.error && j.error.message) || j.error || j.message; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
async function rpc(method, params) {
  const r = await api('rpc', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({jsonrpc: '2.0', id: Date.now(), method, params})});
  if (r.error) throw new Error(r.error.message);
  return r.result;
}
async function node(hash) {
  if (!hash) return null;
  if (!S.nodes[hash]) S.nodes[hash] = await api(`api/node/${hash}`);
  return S.nodes[hash];
}
function toast(msg) {
  const t = document.createElement('div');
  t.className = 'toast'; t.setAttribute('role', 'alert'); t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 4000);
}

/* ---------- Status Glyphs: 16px inline SVG using theme CSS variables ---------- */
const GL = {
  prep: 'Preparing & prechecking', wait: 'Awaiting confirmation', run: 'Running', paused: 'Paused', done: 'Done (accepted)',
  'done-na': 'Done (acceptance N/A)', bad: 'Action required', stopped: 'Stopped', none: 'Not started', dropped: 'Dropped',
  srun: 'In progress', pass: 'Pass', warn: 'Warning', fail: 'Fail', info: 'Info',
};
function glyph(kind, label, frac) {
  const t = h(label || GL[kind] || kind);
  let g = '';
  const C = 2 * Math.PI * 6;
  switch (kind) {
    case 'prep': g = `<g class="spin"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--gl-mute)" stroke-width="1.6" stroke-dasharray="2.4 2.3"/></g>`; break;
    case 'wait': g = `<circle cx="8" cy="8" r="4.5" fill="var(--accent)"/>`; break;
    case 'run': {
      const f = Math.max(0.07, Math.min(1, frac || 0));
      g = `<circle cx="8" cy="8" r="6" fill="none" stroke="var(--gl-track)" stroke-width="2"/>
        <circle cx="8" cy="8" r="6" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round"
          stroke-dasharray="${(f * C).toFixed(2)} ${C.toFixed(2)}" transform="rotate(-90 8 8)"/>`;
      break;
    }
    case 'paused': g = `<circle cx="8" cy="8" r="6.3" fill="none" stroke="var(--gl-mute)" stroke-width="1.5"/>
      <rect x="5.6" y="5" width="1.6" height="6" rx=".5" fill="var(--gl-mute)"/><rect x="8.8" y="5" width="1.6" height="6" rx=".5" fill="var(--gl-mute)"/>`; break;
    case 'done': g = `<circle cx="8" cy="8" r="7" fill="var(--ok)"/><path d="M4.9 8.2l2.1 2.1 4.1-4.3" fill="none" stroke="var(--on-ok)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`; break;
    case 'done-na': g = `<circle cx="8" cy="8" r="6.3" fill="none" stroke="var(--ok)" stroke-width="1.3"/><path d="M5.1 8.2l2 2 3.9-4.1" fill="none" stroke="var(--ok)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`; break;
    case 'bad': g = `<circle cx="8" cy="8" r="7" fill="var(--bad)"/><path d="M5 8h6" stroke="var(--on-ok)" stroke-width="1.8" stroke-linecap="round"/>`; break;
    case 'stopped': g = `<circle cx="8" cy="8" r="6.3" fill="none" stroke="var(--gl-mute)" stroke-width="1.5"/><rect x="5.5" y="5.5" width="5" height="5" rx=".8" fill="var(--gl-mute)"/>`; break;
    case 'none': g = `<circle cx="8" cy="8" r="5.5" fill="none" stroke="var(--gl-track)" stroke-width="1.5"/>`; break;
    case 'dropped': g = `<circle cx="8" cy="8" r="5.5" fill="none" stroke="var(--gl-mute)" stroke-width="1.5"/><path d="M4.4 11.6l7.2-7.2" stroke="var(--gl-mute)" stroke-width="1.5" stroke-linecap="round"/>`; break;
    case 'srun': g = `<g class="spin"><path d="M8 3a5 5 0 1 1-5 5" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/></g>`; break;
    case 'pass': g = `<path d="M3.6 8.4l2.9 2.9 5.9-6.3" fill="none" stroke="var(--ok)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>`; break;
    case 'warn': g = `<circle cx="8" cy="8" r="4.2" fill="var(--warn)"/>`; break;
    case 'fail': g = `<path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="var(--bad)" stroke-width="1.9" stroke-linecap="round"/>`; break;
    default: g = `<circle cx="8" cy="8" r="2.4" fill="var(--gl-mute)"/>`;
  }
  return `<svg class="gl" width="16" height="16" viewBox="0 0 16 16" role="img" aria-label="${t}"><title>${t}</title>${g}</svg>`;
}
/* Map evidence/check verdicts to glyphs */
const VERDICT = {pass: ['pass', 'Pass'], warn: ['warn', 'Pass with warning'], fail: ['fail', 'Fail'], error: ['fail', 'Evaluation error'],
  override: ['warn', 'Failed, manually overridden'], na: ['info', 'N/A'], info: ['info', 'Info'], running: ['srun', 'Running'], none: ['none', 'Not checked']};
const vglyph = st => { const [k, l] = VERDICT[st] || VERDICT.none; return glyph(k, l); };

/* ---------- Icons ---------- */
const IC = {
  search: '<circle cx="9" cy="9" r="5.6"/><path d="M13.2 13.2l3.6 3.6"/>',
  side: '<rect x="3" y="4" width="14" height="12" rx="2.5"/><path d="M8 4v12"/>',
  panel: '<rect x="3" y="4" width="14" height="12" rx="2.5"/><path d="M12 4v12"/>',
  pen: '<path d="M10 3.5H6A2.5 2.5 0 0 0 3.5 6v8A2.5 2.5 0 0 0 6 16.5h8a2.5 2.5 0 0 0 2.5-2.5v-4"/><path d="M14.6 3.2a1.6 1.6 0 0 1 2.2 2.2L10.5 11.7 7.8 12.3l.6-2.7z"/>',
  more: '<circle cx="5" cy="10" r=".9" fill="currentColor"/><circle cx="10" cy="10" r=".9" fill="currentColor"/><circle cx="15" cy="10" r=".9" fill="currentColor"/>',
  plus: '<path d="M10 4.5v11M4.5 10h11"/>',
  clip: '<path d="M15.5 9.6l-5.7 5.7a3.3 3.3 0 0 1-4.7-4.7l6-6a2.2 2.2 0 0 1 3.1 3.1l-5.9 5.9a1.1 1.1 0 0 1-1.6-1.6l5.3-5.2"/>',
  talk: '<path d="M4 5.5h12v8H9l-3 2.5v-2.5H4z"/>',
  up: '<path d="M10 15.5v-11M5.5 9L10 4.5 14.5 9"/>',
  menu: '<path d="M3.5 7h13M3.5 13h9"/>',
  close: '<path d="M5.5 5.5l9 9M14.5 5.5l-9 9"/>',
  undo: '<path d="M7.5 5L4 8.5 7.5 12"/><path d="M4.5 8.5h7a4 4 0 0 1 0 8H9"/>',
  minus: '<path d="M4.5 10h11"/>',
  chev: '<path d="M7.5 5l5 5-5 5"/>',
  down: '<path d="M6 8l4 4 4-4"/>',
  eye: '<path d="M2.5 10s2.8-5 7.5-5 7.5 5 7.5 5-2.8 5-7.5 5-7.5-5-7.5-5z"/><circle cx="10" cy="10" r="2.3"/>',
  eyeoff: '<path d="M2.5 10s2.8-5 7.5-5 7.5 5 7.5 5-2.8 5-7.5 5-7.5-5-7.5-5z"/><circle cx="10" cy="10" r="2.3"/><path d="M4 16L16 4"/>',
  check: '<path d="M4.5 10.5l3.5 3.5 7.5-8"/>',
  doc: '<path d="M6 2.8h5.2L15 6.6V16a1.2 1.2 0 0 1-1.2 1.2H6A1.2 1.2 0 0 1 4.8 16V4A1.2 1.2 0 0 1 6 2.8z"/><path d="M11 3v4h4M7.5 10.5h5M7.5 13.5h5"/>',
  open: '<path d="M11 4h5v5M16 4l-7 7M14 12v3.5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3.5"/>',
  list: '<path d="M8 5.5h8.5M8 10h8.5M8 14.5h8.5"/><path d="M3.2 5.5l1 1 1.8-2M3.2 10l1 1 1.8-2M3.2 14.5l1 1 1.8-2"/>',
  auto: '<path d="M15.5 8.5A5.8 5.8 0 0 0 5 6.2M4.5 11.5A5.8 5.8 0 0 0 15 13.8"/><path d="M4.6 3.3v3.2h3.2M15.4 16.7v-3.2h-3.2"/>',
  table: '<rect x="3" y="4" width="14" height="12" rx="2"/><path d="M3 8h14M3 12h14M8.5 8v8"/>',
  back: '<path d="M12.5 5l-5 5 5 5"/>',
  sun: '<circle cx="10" cy="10" r="3.4"/><path d="M10 2.5v1.8M10 15.7v1.8M2.5 10h1.8M15.7 10h1.8M4.7 4.7l1.3 1.3M14 14l1.3 1.3M4.7 15.3L6 14M14 6l1.3-1.3"/>',
  moon: '<path d="M15.8 12.3A6.5 6.5 0 0 1 7.7 4.2a6.5 6.5 0 1 0 8.1 8.1z"/>',
  sys: '<rect x="2.8" y="4" width="14.4" height="9.6" rx="1.6"/><path d="M7.5 16.5h5M10 13.6v2.9"/>',
};
const ic = (k, cls = '') => `<svg class="ic ${cls}" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[k]}</svg>`;
const SOLID = {
  pause: '<rect x="5.5" y="4.5" width="3" height="11" rx="1"/><rect x="11.5" y="4.5" width="3" height="11" rx="1"/>',
  stop: '<rect x="5.5" y="5.5" width="9" height="9" rx="1.6"/>',
  play: '<path d="M7 4.6v10.8a.6.6 0 0 0 .9.5l8.2-5.4a.6.6 0 0 0 0-1L7.9 4.1a.6.6 0 0 0-.9.5z"/>',
};
const icS = k => `<svg class="ic" width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">${SOLID[k]}</svg>`;

/* ---------- Markdown Rendering ---------- */
function inlineMd(s) {
  const codes = [];
  const out = h(s).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; })
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])/g, '$1<i>$2</i>')
    .replace(/~~(.+?)~~/g, '<s>$1</s>');
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}
const MD_ITEM = /^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$/;
/* Parse nested Markdown lists starting at line i */
function mdList(lines, i) {
  const first = MD_ITEM.exec(lines[i]), base = first[1].length, ordered = first[2] !== undefined, items = [];
  while (i < lines.length) {
    const fence = /^(\s*)(`{3,}|~{3,})/.exec(lines[i]);
    if (fence && items.length && fence[1].length >= base + 2) {
      const code = [], pad = fence[1].length;
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[2]); i++) code.push(lines[i].slice(Math.min(pad, /^\s*/.exec(lines[i])[0].length)));
      i++;
      items[items.length - 1].sub += `<pre class="md-code"><code>${h(code.join('\n'))}</code></pre>`;
      continue;
    }
    const m = MD_ITEM.exec(lines[i]);
    if (m && m[1].length >= base + 2 && items.length) {
      const [sub, j] = mdList(lines, i);
      items[items.length - 1].sub += sub;
      i = j;
    } else if (m && m[1].length >= base && (m[2] !== undefined) === ordered) {
      items.push({text: [m[3]], sub: ''});
      i++;
    } else if (m) {
      break;
    } else if (!lines[i].trim()) {
      let j = i;
      while (j < lines.length && !lines[j].trim()) j++;
      const n = j < lines.length && MD_ITEM.exec(lines[j]);
      const inside = j < lines.length && /^\s*/.exec(lines[j])[0].length >= base + 2;
      if (!inside && (!n || n[1].length < base || (n[1].length < base + 2 && (n[2] !== undefined) !== ordered))) break;
      i = j;
    } else if (/^\s{2,}\S/.test(lines[i]) && items.length) {
      items[items.length - 1].text.push(lines[i].trim());
      i++;
    } else {
      break;
    }
  }
  const tag = ordered ? 'ol' : 'ul', start = ordered && +first[2] !== 1 ? ` start="${+first[2]}"` : '';
  return [`<${tag}${start}>${items.map(x => `<li>${x.text.map(inlineMd).join('<br>')}${x.sub}</li>`).join('')}</${tag}>`, i];
}
const mdCells = l => l.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '').split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, '|'));
const mdRule = l => /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(l) && l.includes('|');
function prose(text) {
  const lines = String(text || '').replace(/\r/g, '').split('\n'), out = [];
  let para = [], i = 0;
  const flushP = () => { if (para.length) { out.push(`<p>${para.map(inlineMd).join('<br>')}</p>`); para = []; } };
  while (i < lines.length) {
    const l = lines[i].trimEnd();
    let m;
    if ((m = /^\s*(`{3,}|~{3,})/.exec(l))) {
      flushP();
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(m[1]); i++) code.push(lines[i]);
      i++;
      out.push(`<pre class="md-code"><code>${h(code.join('\n'))}</code></pre>`);
    } else if (!l.trim()) {
      flushP();
      i++;
    } else if ((m = /^(#{1,6})\s+(.*?)\s*#*$/.exec(l))) {
      flushP();
      const n = Math.min(m[1].length + 2, 6);
      out.push(`<h${n} class="md-h">${inlineMd(m[2])}</h${n}>`);
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) {
      flushP();
      out.push('<hr>');
      i++;
    } else if (l.includes('|') && i + 1 < lines.length && mdRule(lines[i + 1])) {
      flushP();
      const head = mdCells(l), rows = [];
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) rows.push(mdCells(lines[i]));
      out.push(`<div class="md-tbl"><table><thead><tr>${head.map(c => `<th>${inlineMd(c)}</th>`).join('')}</tr></thead><tbody>${
        rows.map(r => `<tr>${head.map((_, k) => `<td>${inlineMd(r[k] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
    } else if (/^\s*>/.test(l)) {
      flushP();
      const q = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) q.push(lines[i].replace(/^\s*>\s?/, ''));
      out.push(`<blockquote>${prose(q.join('\n'))}</blockquote>`);
    } else if (MD_ITEM.test(l)) {
      flushP();
      const [html, j] = mdList(lines, i);
      out.push(html);
      i = j;
    } else {
      para.push(l.trim());
      i++;
    }
  }
  flushP();
  return out.join('');
}
