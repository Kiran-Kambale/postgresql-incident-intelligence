// App shell: hash router, navigation, global controls (instance, range, refresh, timezone, theme), banners and the refresh loop.
import { state, set, subscribe, PRESETS, range } from './state.js';
import { api, get, onAuthNeeded } from './api.js';
import { h, icon, badge, toast, errorBox, skeleton, closeDrawer, drawerOpen, formDialog } from './ui.js';

const PAGES = [
  { id: 'overview', label: 'Overview', icon: 'overview', group: 'Investigate' }, { id: 'activity', label: 'Live Activity', icon: 'activity' }, { id: 'queries', label: 'Query Performance', icon: 'queries' },
  { id: 'locks', label: 'Locks and Blocking', icon: 'locks' }, { id: 'vacuum', label: 'Transactions and Vacuum', icon: 'vacuum' }, { id: 'replication', label: 'Replication and HA', icon: 'replication' },
  { id: 'incidents', label: 'Incidents', icon: 'incidents', group: 'Respond', badge: 'incidents' }, { id: 'evidence', label: 'Evidence Explorer', icon: 'evidence', badge: 'evidence' },
  { id: 'instances', label: 'Database Instances', icon: 'instances', group: 'Configure' }, { id: 'settings', label: 'Monitoring Settings', icon: 'settings' }, { id: 'audit', label: 'Audit and Diagnostics', icon: 'audit' },
];
const loaders = Object.fromEntries(PAGES.map(p => [p.id, () => import(`./pages/${p.id}.js`)]));
const view = document.getElementById('view');
let current = null; let renderToken = 0; let timer = null; let navBadges = {};
const TZS = ['UTC', 'browser', 'Asia/Kolkata', 'Europe/Berlin', 'Europe/London', 'America/New_York', 'America/Los_Angeles', 'Asia/Tokyo', 'Australia/Sydney'];

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, ''); const [path, query = ''] = raw.split('?'); const [page, ...rest] = path.split('/');
  return { page: loaders[page] ? page : 'overview', arg: rest.length ? decodeURIComponent(rest.join('/')) : '', qs: new URLSearchParams(query) };
}

function drawNav(active) {
  const nav = document.getElementById('nav'); nav.replaceChildren();
  PAGES.forEach(p => {
    if (p.group) nav.append(h('div', { class: 'sep' }, p.group));
    const n = p.badge ? navBadges[p.badge] : 0;
    nav.append(h('a', { href: `#/${p.id}`, 'data-page': p.id, 'aria-current': p.id === active ? 'page' : null, onclick: () => document.getElementById('sidebar').classList.remove('open') }, icon(p.icon), p.label, n ? h('span', { class: `badge ${p.badge === 'incidents' ? 'b-critical' : 'b-critical'}` }, String(n)) : null));
  });
}

function drawRange() {
  const box = document.getElementById('range'); box.replaceChildren();
  const seg = h('div', { class: 'seg' }, [...Object.keys(PRESETS), 'custom'].map(k => h('button', { type: 'button', 'aria-pressed': String(state.rangePreset === k), disabled: k === 'custom' && !state.customRange, title: k === 'custom' ? 'Set by dragging on a chart or timeline' : `Last ${k}`,
    onclick: () => set({ rangePreset: k }) }, k === 'custom' ? 'Custom' : k)));
  box.append(seg);
}

async function drawControls() {
  const sel = document.getElementById('instance');
  try {
    const d = await api('/api/instances');
    sel.replaceChildren(...d.items.map(i => h('option', { value: i.id, selected: i.id === state.instance }, `${i.name}${i.demo ? ' (demo)' : ''}`)));
    if (!d.items.some(i => i.id === state.instance)) set({ instance: d.items[0]?.id || '' }, { notify: false });
    sel.value = state.instance; sel.disabled = d.items.length < 2;
    document.getElementById('banners').replaceChildren(...[
      state.server.demo ? h('div', { class: 'banner demo', role: 'status' }, h('strong', 'DEMO DATA.'), 'Everything shown is synthetic and stored separately from live observations. It does not describe any real database.') : null,
      ...(d.warnings || []).map(w => h('div', { class: 'banner warn' }, icon('info', 16), w))].filter(Boolean));
  } catch { /* shown by the page itself */ }
}

async function bootServer() {
  try { const h0 = await fetch('/api/auth/status').then(r => r.json()); state.server.authRequired = h0.authentication_required; document.getElementById('auth').hidden = !h0.authentication_required; } catch { /* offline */ }
  try { const hl = await api('/api/health'); state.server.demo = hl.demo_mode; document.getElementById('ver').textContent = `v${hl.version} · read-only diagnostics`; } catch { document.getElementById('ver').textContent = 'v1.0 · read-only diagnostics'; }
}

async function refreshBadges() {
  try { const o = await get('/api/live/overview', { instance: state.instance || undefined }); navBadges = { incidents: (o.incidents.counts.candidate || 0) + (o.incidents.counts.open || 0) + (o.incidents.counts.investigating || 0), evidence: o.evidence.counts.active_by_severity.critical || 0 }; drawNav(parseHash().page); setDot('on'); }
  catch { setDot('err'); }
}
function setDot(kind) { const d = document.getElementById('livedot'); d.className = `livedot ${kind}`; d.title = kind === 'on' ? 'Dashboard is receiving data' : 'Cannot reach the server'; }

async function show() {
  const { page, arg, qs } = parseHash(); const my = ++renderToken;
  drawNav(page); document.title = `${PAGES.find(p => p.id === page).label} · DB Incident Replay`;
  view.replaceChildren(skeleton(3, 90)); current = null;
  const ctx = { page, arg, qs, tick: null, reload: show };
  try {
    const mod = await loaders[page]();
    const node = await mod.render(ctx);
    if (my !== renderToken) return;
    view.replaceChildren(node); current = ctx; view.focus({ preventScroll: true });
  } catch (e) { if (my === renderToken) view.replaceChildren(errorBox(e.status !== undefined ? e : { message: e.message || String(e) }, show)); }
}

function startTimer() {
  clearInterval(timer);
  if (!state.refreshSec) return;
  timer = setInterval(() => { if (document.hidden || drawerOpen() || document.querySelector('dialog[open]')) return; refreshBadges(); if (current && current.tick) Promise.resolve(current.tick()).catch(() => {}); }, state.refreshSec * 1000);
}

function init() {
  document.documentElement.dataset.theme = state.theme;
  document.getElementById('menu').append(icon('menu')); document.getElementById('drawer-close').append(icon('close', 16));
  const themeBtn = document.getElementById('theme'); const drawTheme = () => themeBtn.replaceChildren(icon(state.theme === 'dark' ? 'sun' : 'moon'));
  drawTheme(); themeBtn.addEventListener('click', () => { set({ theme: state.theme === 'dark' ? 'light' : 'dark' }, { notify: false }); document.documentElement.dataset.theme = state.theme; drawTheme(); });
  const tz = document.getElementById('tz'); tz.replaceChildren(...TZS.map(z => h('option', { value: z, selected: z === state.tz }, z === 'browser' ? `Browser (${Intl.DateTimeFormat().resolvedOptions().timeZone})` : z))); tz.addEventListener('change', () => { set({ tz: tz.value }); show(); });
  const rf = document.getElementById('refresh'); rf.value = String(state.refreshSec); rf.addEventListener('change', () => { set({ refreshSec: Number(rf.value) }, { notify: false }); startTimer(); });
  document.getElementById('instance').addEventListener('change', e => set({ instance: e.target.value }));
  document.getElementById('menu').addEventListener('click', () => { const sb = document.getElementById('sidebar'); sb.classList.toggle('open'); document.getElementById('menu').setAttribute('aria-expanded', String(sb.classList.contains('open'))); });
  document.getElementById('drawer-close').addEventListener('click', closeDrawer); document.getElementById('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && drawerOpen() && !document.querySelector('dialog[open]')) { closeDrawer(); return; }
    if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '') && !document.querySelector('dialog[open]')) { const s = document.querySelector('[data-search]'); if (s) { e.preventDefault(); s.focus(); } }
  });
  onAuthNeeded(async () => {
    if (document.querySelector('dialog[open]')) return;
    const out = await formDialog({ title: 'API token required', intro: 'This server requires authentication. The token is kept in this browser tab only (sessionStorage).', submitText: 'Use token', fields: [{ name: 'token', label: 'API token', type: 'password', required: true }] });
    if (out) { set({ token: out.token.trim() }); bootServer().then(show); }
  });
  document.getElementById('auth').addEventListener('click', () => state.token ? (set({ token: '' }), toast('Signed out', 'ok'), show()) : document.dispatchEvent(new Event('dir-auth')));
  document.addEventListener('dir-auth', () => { api('/api/health').catch(() => {}); });
  subscribe(patch => { if ('rangePreset' in patch || 'customRange' in patch) drawRange(); if ('instance' in patch || 'rangePreset' in patch || 'customRange' in patch) { drawControls(); show(); } });
  window.addEventListener('hashchange', () => { closeDrawer(); show(); });
  bootServer().then(async () => { await drawControls(); drawRange(); await show(); refreshBadges(); startTimer(); });
}
init();
