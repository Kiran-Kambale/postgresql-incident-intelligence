// Small DOM toolkit: hyperscript, formatters and reusable components. Data is never injected as HTML.
import { state } from './state.js';

export function h(tag, attrs, ...kids) {
  if (attrs && (attrs instanceof Node || typeof attrs !== 'object' || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, kids); return el;
}
export function append(el, kids) { for (const k of [kids].flat(Infinity)) { if (k == null || k === false) continue; el.append(k instanceof Node ? k : document.createTextNode(String(k))); } return el; }
const NS = 'http://www.w3.org/2000/svg';
export function s(tag, attrs, ...kids) {
  if (attrs && (attrs instanceof Node || typeof attrs !== 'object' || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) { if (v == null || v === false) continue; if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v); else el.setAttribute(k, v); }
  append(el, kids); return el;
}
const ICONS = {
  overview: 'M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10', activity: 'M3 12h4l3-8 4 16 3-8h4', queries: 'M4 6h16M4 12h10M4 18h7M17 15l3 3-3 3', locks: 'M6 11V8a6 6 0 0112 0v3M5 11h14v10H5z',
  vacuum: 'M4 7h16M6 7l1 13h10l1-13M9 4h6', replication: 'M7 7h10l-3-3M17 17H7l3 3M4 12h16', incidents: 'M12 3l10 18H2zM12 10v5M12 18v.01', evidence: 'M4 4h16v16H4zM8 9h8M8 13h8M8 17h5',
  instances: 'M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01', settings: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19 12l2-1-2-4-2 .5-1.5-1L15 4h-4l-.5 2.5-1.5 1L7 7 5 11l2 1v2l-2 1 2 4 2-.5 1.5 1L11 20h4l.5-2.5 1.5-1 2 .5 2-4-2-1z',
  audit: 'M9 12l2 2 4-4M5 3h14v18H5z', menu: 'M4 6h16M4 12h16M4 18h16', close: 'M6 6l12 12M18 6L6 18', sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4M12 8a4 4 0 100 8 4 4 0 000-8z',
  moon: 'M21 13A9 9 0 1111 3a7 7 0 0010 10z', refresh: 'M20 11a8 8 0 10-2.3 6M20 4v7h-7', copy: 'M9 9h11v11H9zM5 15V5h10', link: 'M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1',
  plus: 'M12 5v14M5 12h14', down: 'M6 9l6 6 6-6', up: 'M6 15l6-6 6 6', ext: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6', search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM21 21l-5-5', info: 'M12 8v.01M11 12h1v5h1M12 2a10 10 0 100 20 10 10 0 000-20z',
};
export function icon(name, size = 18) { return s('svg', { viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.9, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, s('path', { d: ICONS[name] || '' })); }

// ---------------------------------------------------------------- formatters
export const num = (n, d = 0) => (n == null || Number.isNaN(n)) ? '–' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
export function dur(sec) {
  if (sec == null) return '–'; sec = Math.round(sec);
  if (sec < 60) return `${sec}s`; const m = Math.floor(sec / 60), r = sec % 60;
  if (m < 60) return `${m}m ${r}s`; const hh = Math.floor(m / 60), mm = m % 60;
  if (hh < 48) return `${hh}h ${mm}m`; return `${Math.floor(hh / 24)}d ${hh % 24}h`;
}
export function ms(v) { return v == null ? '–' : v >= 1000 ? dur(v / 1000) : `${v < 10 ? v.toFixed(2) : v.toFixed(1)} ms`; }
export function bytes(n) { if (n == null) return '–'; const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let i = 0; n = Number(n); while (Math.abs(n) >= 1024 && i < 4) { n /= 1024; i++; } return `${i ? n.toFixed(1) : n.toFixed(0)} ${u[i]}`; }
export function tzOptions(tz) {
  const o = { hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' };
  if (tz && tz !== 'browser') o.timeZone = tz; return o;
}
export function fmtTime(iso, withDate = true) {
  if (!iso) return '–'; const d = new Date(iso); if (Number.isNaN(d.getTime())) return String(iso);
  const o = tzOptions(state.tz); if (!withDate) { delete o.year; delete o.month; delete o.day; }
  try { return new Intl.DateTimeFormat('en-CA', o).format(d).replace(',', ''); } catch { return d.toISOString(); }
}
export function relTime(iso) { if (!iso) return ''; const sec = (Date.now() - new Date(iso).getTime()) / 1000; return sec < 0 ? 'in the future' : sec < 5 ? 'just now' : `${dur(sec)} ago`; }
export function timeEl(iso, { rel = false } = {}) {
  if (!iso) return h('span', { class: 'muted' }, '–');
  const el = h('time', { datetime: iso, class: 'nowrap', title: `UTC ${iso}\nLocal ${new Date(iso).toLocaleString()}\nClick to copy the UTC timestamp`, style: { cursor: 'copy' }, onclick: e => { e.stopPropagation(); copy(iso, 'UTC timestamp copied'); } }, fmtTime(iso));
  return rel ? h('span', el, h('span', { class: 'muted small' }, ` · ${relTime(iso)}`)) : el;
}
export const tzLabel = () => (state.tz === 'browser' ? Intl.DateTimeFormat().resolvedOptions().timeZone : state.tz);

// ---------------------------------------------------------------- small components
export const badge = (text, kind = text) => h('span', { class: `badge b-${String(kind).toLowerCase().replace(/[^a-z_]/g, '')}` }, text);
export const sev = s => h('span', { class: 'nowrap' }, h('span', { class: `dot ${s}` }), badge(s));
export const tip = (text, label) => h('span', { 'data-tip': text, tabindex: 0 }, label);
export function copy(text, msg = 'Copied') {
  const done = () => toast(msg, 'ok');
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(String(text)).then(done, () => fallbackCopy(text, done));
  else fallbackCopy(text, done);
}
function fallbackCopy(text, done) { const t = h('textarea', { style: { position: 'fixed', opacity: 0 } }); t.value = text; document.body.append(t); t.select(); try { document.execCommand('copy'); done(); } finally { t.remove(); } }
export function copyBtn(text, label = 'Copy') { return h('button', { class: 'btn small', type: 'button', title: 'Copy to clipboard', onclick: e => { e.stopPropagation(); copy(text); } }, icon('copy', 14), label); }
export function toast(msg, kind = '') { const t = h('div', { class: `toast ${kind}` }, msg); document.getElementById('toasts').append(t); setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3200); }
export const skeleton = (rows = 3, height = 70) => h('div', { class: 'grid', 'aria-busy': 'true', 'aria-label': 'Loading' }, Array.from({ length: rows }, () => h('div', { class: 'skel', style: { height: `${height}px` } })));
export const empty = (title, text, action) => h('div', { class: 'empty' }, h('h3', title), h('div', text), action ? h('div', { class: 'mt' }, action) : null);
export function errorBox(err, retry) {
  return h('div', { class: 'errorbox', role: 'alert' }, h('h3', err.code === 'NOT_CONFIGURED' ? 'No database instance configured' : 'Something went wrong'), h('div', err.message || String(err)),
    err.hint ? h('div', { class: 'muted mt' }, err.hint) : null, err.requestId ? h('div', { class: 'muted small mt' }, `Request id: ${err.requestId}`) : null, retry ? h('div', { class: 'mt' }, h('button', { class: 'btn', onclick: retry }, icon('refresh', 14), 'Retry')) : null);
}
export const callout = (kind, ...kids) => h('div', { class: `callout ${kind || ''}` }, kids);
export function limitations(items) { return items && items.length ? h('details', { class: 'mt small muted' }, h('summary', 'Limitations of this data'), h('ul', items.map(i => h('li', i)))) : null; }
export function metaLine(meta) {
  if (!meta) return null;
  const bits = [];
  if (meta.status === 'pending') bits.push('Waiting for the first collection…');
  else { bits.push(meta.collected_at ? ['Collected ', timeEl(meta.collected_at), meta.age_s != null ? ` (${dur(meta.age_s)} ago)` : ''] : 'Not collected'); if (meta.stale) bits.push(badge('stale', 'stale')); if (meta.status && !['ok', 'pending'].includes(meta.status)) bits.push(badge(meta.status, meta.status === 'error' ? 'critical' : 'warning')); }
  return h('div', { class: 'muted small' }, bits.map((b, i) => [i ? ' · ' : '', b]));
}
export function kpi({ label, value, hint, tone, onclick, tipText, bar, na }) {
  return h('button', { class: `card kpi ${tone || ''} ${na ? 'na' : ''}`, onclick, type: 'button' },
    h('div', { class: 'label' }, tipText ? tip(tipText, label) : label), h('div', { class: 'value' }, na ? 'unavailable' : value),
    hint ? h('div', { class: 'hint' }, hint) : null, bar != null ? h('div', { class: `bar ${tone || ''}` }, h('i', { style: { width: `${Math.min(100, bar)}%` } })) : null);
}
export function kv(pairs) { return h('dl', { class: 'kv' }, pairs.filter(Boolean).flatMap(([k, v]) => [h('dt', k), h('dd', v ?? '–')])); }
export function jsonView(obj) { return h('pre', { class: 'json', tabindex: 0 }, JSON.stringify(obj, null, 2)); }
export function tabs(defs, initial, onChange) {
  const bar = h('div', { class: 'tabs', role: 'tablist' }); const body = h('div'); let cur = initial || defs[0].id;
  const draw = () => { bar.replaceChildren(...defs.map(d => h('button', { role: 'tab', 'aria-selected': String(d.id === cur), onclick: () => { cur = d.id; onChange && onChange(cur); draw(); } }, d.label))); const d = defs.find(x => x.id === cur); body.replaceChildren(typeof d.render === 'function' ? d.render() : d.render); };
  draw(); return h('div', bar, body);
}
export function chips(options, selected, onToggle) {
  return h('div', { class: 'chips' }, options.map(o => h('button', { class: 'chip', type: 'button', 'aria-pressed': String(selected.has(o.value ?? o)), onclick: () => onToggle(o.value ?? o) }, o.label ?? o)));
}
export const debounce = (fn, wait = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), wait); }; };

// ---------------------------------------------------------------- tables
export function dataTable({ columns, rows, sort, onSort, onRow, expand, empty: emptyNode, caption }) {
  const table = h('table', caption ? h('caption', { class: 'sr' }, caption) : null);
  const head = h('tr', columns.map(c => {
    const active = sort && sort.key === c.key;
    const label = c.tip ? tip(c.tip, c.label) : c.label;
    return h('th', { scope: 'col', class: c.num ? 'right' : '', 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : null },
      c.sortable && onSort ? h('button', { type: 'button', onclick: () => onSort(c.key) }, label, active ? icon(sort.dir === 'asc' ? 'up' : 'down', 12) : null) : label);
  }));
  const tbody = h('tbody');
  rows.forEach(row => {
    const tr = h('tr', { class: onRow || expand ? 'clickable' : '', tabindex: onRow || expand ? 0 : null }, columns.map(c => h('td', { class: c.num ? 'num' : '' }, c.render ? c.render(row) : row[c.key] ?? '–')));
    let open = null;
    const toggle = () => {
      if (expand) { if (open) { open.remove(); open = null; tr.classList.remove('expanded'); } else { open = h('tr', { class: 'expanded' }, h('td', { colspan: columns.length }, expand(row))); tr.after(open); tr.classList.add('expanded'); } }
      if (onRow) onRow(row);
    };
    if (onRow || expand) { tr.addEventListener('click', e => { if (!e.target.closest('button,a,input,select')) toggle(); }); tr.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target === tr) { e.preventDefault(); toggle(); } }); }   /* preventDefault above: focus moves into the drawer on Enter, and the follow-up keypress must not activate its close button */
    tbody.append(tr);
  });
  if (!rows.length) tbody.append(h('tr', h('td', { colspan: columns.length }, emptyNode || empty('Nothing to show', 'No rows match the current filters.'))));
  table.append(h('thead', head), tbody);
  return h('div', { class: 'tablewrap' }, table);
}
export function pager({ page, pages, total, pageSize, onPage }) {
  return h('div', { class: 'pager' }, h('span', `${num(total)} result${total === 1 ? '' : 's'} · page ${page} of ${pages} · ${pageSize} per page`),
    h('span', { class: 'row' }, h('button', { class: 'btn small', disabled: page <= 1, onclick: () => onPage(page - 1) }, 'Previous'), h('button', { class: 'btn small', disabled: page >= pages, onclick: () => onPage(page + 1) }, 'Next')));
}

// ---------------------------------------------------------------- drawer + dialogs
let lastFocus = null; let onDrawerClose = null;
export function openDrawer(title, body, { onClose } = {}) {
  lastFocus = document.activeElement; onDrawerClose = onClose;
  document.getElementById('drawer-title').textContent = title; document.getElementById('drawer-body').replaceChildren(body);
  document.getElementById('drawer').hidden = false; document.getElementById('scrim').hidden = false; document.getElementById('drawer-close').focus();
}
export function closeDrawer() {
  const d = document.getElementById('drawer'); if (d.hidden) return; d.hidden = true; document.getElementById('scrim').hidden = true;
  if (onDrawerClose) onDrawerClose(); onDrawerClose = null; if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}
export const drawerOpen = () => !document.getElementById('drawer').hidden;
function runDialog(title, bodyNode, buttons) {
  const dlg = document.getElementById('dlg'); document.getElementById('dlg-title').textContent = title;
  document.getElementById('dlg-body').replaceChildren(bodyNode); const actions = document.getElementById('dlg-actions'); actions.replaceChildren();
  return new Promise(resolve => {
    const finish = v => { dlg.close(); resolve(v); };
    buttons.forEach(b => actions.append(h('button', { class: `btn ${b.cls || ''}`, type: 'button', onclick: () => b.run ? b.run(finish) : finish(b.value) }, b.label)));
    dlg.addEventListener('cancel', () => resolve(null), { once: true }); dlg.showModal();
  });
}
export const confirmDialog = ({ title, body, confirmText = 'Confirm', danger = false }) => runDialog(title, body instanceof Node ? body : h('p', body), [{ label: 'Cancel', value: false }, { label: confirmText, cls: danger ? 'danger' : 'primary', value: true }]).then(v => v === true);
export function formDialog({ title, fields, submitText = 'Save', intro }) {
  const inputs = {}; const form = h('div', intro ? h('p', { class: 'muted' }, intro) : null, fields.map(f => {
    let el;
    if (f.type === 'textarea') el = h('textarea', { rows: f.rows || 4, maxlength: f.max || 4000 }, f.value || '');
    else if (f.type === 'select') el = h('select', f.options.map(o => h('option', { value: o.value ?? o, selected: (o.value ?? o) === f.value }, o.label ?? o)));
    else el = h('input', { type: f.type || 'text', value: f.value ?? '', maxlength: f.max || 300, placeholder: f.placeholder || '' });
    inputs[f.name] = el; const id = `f-${f.name}`; el.id = id;
    return h('div', { class: 'field' }, h('label', { for: id }, f.label + (f.required ? ' *' : '')), el, f.hint ? h('span', { class: 'hint' }, f.hint) : null);
  }), h('div', { class: 'callout crit', id: 'form-err', hidden: true }));
  return runDialog(title, form, [{ label: 'Cancel', value: null }, { label: submitText, cls: 'primary', run: finish => {
    const out = {}; for (const f of fields) { out[f.name] = inputs[f.name].value; if (f.required && !String(out[f.name]).trim()) { const e = form.querySelector('#form-err'); e.hidden = false; e.textContent = `${f.label} is required.`; inputs[f.name].focus(); return; } }
    finish(out); } }]);
}
