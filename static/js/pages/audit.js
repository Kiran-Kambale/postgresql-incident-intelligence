// Audit and Diagnostics: who did what, collection run history, recorder/store health, retention.
import { get, post, inst } from '../api.js';
import { h, badge, timeEl, dur, num, bytes, dataTable, pager, tabs, kv, errorBox, empty, callout, toast, confirmDialog, jsonView, debounce } from '../ui.js';
import { pageHead, refreshButton } from '../common.js';

export async function render(ctx) {
  const root = h('div'); const body = h('div'); let view = ctx.qs.get('view') || 'runs';
  root.append(pageHead('Audit and Diagnostics', 'Collection history, the audit trail of sensitive operations, and the health of the recorder itself.', refreshButton(load)), body);
  async function runsPanel() {
    const f = { page: 1, tier: '', status: '' }; const box = h('div'); const tier = h('select', { 'aria-label': 'Tier', onchange: () => { f.tier = tier.value; f.page = 1; draw(); } }, [['', 'Any tier'], ['fast', 'fast'], ['standard', 'standard'], ['slow', 'slow']].map(([v, l]) => h('option', { value: v }, l)));
    const st = h('select', { 'aria-label': 'Status', onchange: () => { f.status = st.value; f.page = 1; draw(); } }, [['', 'Any status'], ['ok', 'ok'], ['partial', 'partial'], ['error', 'error']].map(([v, l]) => h('option', { value: v }, l)));
    async function draw() {
      try { const d = await get('/api/runs', inst({ page: f.page, page_size: 15, tier: f.tier || undefined, status: f.status || undefined }));
        box.replaceChildren(dataTable({ caption: 'Collection runs', columns: [{ key: 't', label: 'Started', render: r => timeEl(r.started_at) }, { key: 'tier', label: 'Tier' }, { key: 'trig', label: 'Trigger' }, { key: 's', label: 'Status', render: r => badge(r.status, r.status === 'ok' ? 'ok' : r.status === 'partial' ? 'warning' : 'critical') },
          { key: 'd', label: 'Duration', num: true, render: r => `${r.duration_ms} ms` }, { key: 'c', label: 'Collectors', render: r => h('span', { class: 'small' }, Object.entries(r.collectors).map(([n, c]) => `${n}:${c.status}${c.error_code ? '(' + c.error_code + ')' : ''}`).join('  ')) }], rows: d.items, empty: empty('No runs recorded', 'The recorder has not collected yet.') }),
          pager({ page: d.page, pages: d.pages, total: d.total, pageSize: d.page_size, onPage: p => { f.page = p; draw(); } })); } catch (e) { box.replaceChildren(errorBox(e, draw)); }
    }
    draw(); return h('div', h('div', { class: 'filters' }, tier, st), box);
  }
  function auditPanel() {
    const f = { page: 1, action: '', outcome: '', q: '' }; const box = h('div');
    const q = h('input', { type: 'search', 'data-search': '', placeholder: 'Search actor, action, target…  ( / )', 'aria-label': 'Search audit log' }); q.addEventListener('input', debounce(() => { f.q = q.value; f.page = 1; draw(); }, 300));
    const out = h('select', { 'aria-label': 'Outcome', onchange: () => { f.outcome = out.value; f.page = 1; draw(); } }, [['', 'Any outcome'], ['ok', 'ok'], ['denied', 'denied']].map(([v, l]) => h('option', { value: v }, l)));
    async function draw() {
      try { const d = await get('/api/audit', { page: f.page, page_size: 20, q: f.q || undefined, outcome: f.outcome || undefined });
        box.replaceChildren(dataTable({ caption: 'Audit log', columns: [{ key: 't', label: 'Time', render: r => timeEl(r.ts) }, { key: 'a', label: 'Actor', render: r => `${r.actor} (${r.role})` }, { key: 'ac', label: 'Action', render: r => h('span', { class: 'mono' }, r.action) }, { key: 'tg', label: 'Target', render: r => h('span', { class: 'mono small' }, r.target) },
          { key: 'o', label: 'Outcome', render: r => badge(r.outcome, r.outcome === 'ok' ? 'ok' : 'critical') }, { key: 'ip', label: 'From', render: r => r.ip || '–' }], rows: d.items, expand: r => jsonView(r.details), empty: empty('No audit entries', 'Sensitive operations (incident changes, exports, settings, authorization denials) are recorded here.') }),
          pager({ page: d.page, pages: d.pages, total: d.total, pageSize: d.page_size, onPage: p => { f.page = p; draw(); } })); } catch (e) { box.replaceChildren(errorBox(e.status === 403 ? { ...e, message: 'The audit log needs the admin role.' } : e, draw)); }
    }
    draw(); return h('div', h('div', { class: 'filters' }, q, out), box);
  }
  async function healthPanel() {
    const box = h('div');
    try {
      const s = await get('/api/system/status'); const rec = s.recorder;
      box.append(h('div', { class: 'grid g2' }, h('div', { class: 'card' }, h('h2', 'Flight recorder'), kv([['Running', String(rec.running)], ['Started', rec.started_at ? timeEl(rec.started_at) : '–'], ['Max concurrent collections', rec.max_concurrent], ['Last retention run', rec.last_retention ? `${rec.last_retention.at} (${Object.values(rec.last_retention.deleted).reduce((a, b) => a + b, 0)} rows)` : 'not yet']]),
        Object.entries(rec.instances).map(([id, r]) => h('div', { class: 'mt small' }, h('strong', id), Object.entries(r.tiers).map(([t, x]) => h('div', `${t}: runs ${x.runs}, skipped ${x.skipped}, failures in a row ${x.consecutive_failures}, last ${x.last_status || 'n/a'}`)), Object.keys(r.failure_streaks).length ? callout('warn', 'Failure streaks: ' + JSON.stringify(r.failure_streaks)) : null))),
        h('div', { class: 'card' }, h('h2', 'History store'), kv([['File', s.store.path_name], ['Size', bytes(s.store.size_bytes)], ['Schema version', s.store.schema_version], ['Migrated from version', s.store.migrated_from_version]]), h('h3', { class: 'mt' }, 'Rows'), kv(Object.entries(s.store.rows).map(([k, v]) => [k, num(v)])),
          h('div', { class: 'row mt' }, h('button', { class: 'btn small', type: 'button', onclick: async () => { try { const r = await post('/api/maintenance/retention?dry_run=true'); toast(`Dry run: ${Object.values(r.rows_affected).reduce((a, b) => a + b, 0)} row(s) would be removed`, 'ok'); } catch (e) { toast(e.message, 'err'); } } }, 'Retention dry run'),
            h('button', { class: 'btn small danger', type: 'button', onclick: async () => { if (await confirmDialog({ title: 'Run retention now?', danger: true, confirmText: 'Delete old rows', body: 'Rows older than the configured retention are permanently deleted. Evidence linked to incidents is never removed.' })) { try { const r = await post('/api/maintenance/retention?dry_run=false'); toast(`Removed ${Object.values(r.rows_affected).reduce((a, b) => a + b, 0)} row(s)`, 'ok'); load(); } catch (e) { toast(e.message, 'err'); } } } }, 'Run retention…')))),
        h('div', { class: 'card mt' }, h('h2', 'Configuration (secrets excluded)'), jsonView(s.config)));
    } catch (e) { box.replaceChildren(errorBox(e)); }
    return box;
  }
  async function load() { body.replaceChildren(tabs([{ id: 'runs', label: 'Collection runs', render: runsPanel }, { id: 'audit', label: 'Audit log', render: auditPanel }, { id: 'health', label: 'Recorder and store', render: healthPanel }], view, v => { view = v; })); }
  ctx.tick = null; await load(); return root;
}
