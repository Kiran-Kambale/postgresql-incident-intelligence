// Live Activity: sessions, waits, connection breakdown. Filters are server-side so very large session lists stay responsive.
import { get, inst } from '../api.js';
import { state } from '../state.js';
import { h, badge, dur, num, dataTable, pager, debounce, metaLine, skeleton, errorBox, empty, copyBtn, callout, tabs, kv, chips } from '../ui.js';
import { pageHead, refreshButton, goto } from '../common.js';
import { bars } from '../charts.js';

export async function render(ctx) {
  const q = ctx.qs; const f = { page: 1, size: 25, sort: q.get('sort') || 'query_age_s', order: 'desc', state: q.get('state') || '', q: q.get('q') || '', blocked: false, min: '', pid: q.get('pid') || '', backend: 'client backend' };
  const root = h('div'); const top = h('div'); const tableBox = h('div');
  const search = h('input', { type: 'search', 'data-search': '', placeholder: 'Search query, application, user, PID…  ( / )', 'aria-label': 'Search sessions', value: f.q, style: { minWidth: '260px' } });
  search.addEventListener('input', debounce(() => { f.q = search.value; f.page = 1; load(); }, 300));
  const stateSel = h('select', { 'aria-label': 'State', onchange: () => { f.state = stateSel.value; f.page = 1; load(); } }, [['', 'State: any'], ['active', 'active'], ['idle', 'idle'], ['idle in transaction', 'idle in transaction'], ['idle in transaction (aborted)', 'idle in transaction (aborted)']].map(([v, l]) => h('option', { value: v, selected: f.state === v }, l)));
  const blockedBtn = h('button', { class: 'chip', 'aria-pressed': 'false', type: 'button', onclick: () => { f.blocked = !f.blocked; blockedBtn.setAttribute('aria-pressed', String(f.blocked)); f.page = 1; load(); } }, 'Blocked only');
  const minSel = h('select', { 'aria-label': 'Minimum age', onchange: () => { f.min = minSel.value; f.page = 1; load(); } }, [['', 'Any age'], ['5', '≥ 5s'], ['30', '≥ 30s'], ['300', '≥ 5m'], ['3600', '≥ 1h']].map(([v, l]) => h('option', { value: v }, l)));
  root.append(pageHead('Live Activity', 'Sessions at the last collection. Query text follows the redaction mode set on the server.', refreshButton(load)), top, h('div', { class: 'filters mt' }, search, stateSel, minSel, blockedBtn, f.pid ? badge(`PID ${f.pid}`, 'info') : null), tableBox);
  async function load() {
    try {
      const d = await get('/api/live/activity', inst({ page: f.page, page_size: f.size, sort: f.sort, order: f.order, state: f.state, q: f.q, blocked_only: f.blocked || undefined, min_age_s: f.min || undefined, pid: f.pid || undefined, backend_type: f.backend }));
      const c = d.connections || {};
      top.replaceChildren(h('div', { class: 'grid g3' },
        h('div', { class: 'card' }, h('h2', 'Connections'), c.max_connections ? [h('div', { style: { fontSize: '24px', fontWeight: 700 } }, `${c.slots_used} / ${c.max_connections}`), h('div', { class: `bar ${c.utilization_percent >= 90 ? 'crit' : c.utilization_percent >= 80 ? 'warn' : ''}` }, h('i', { style: { width: `${c.utilization_percent}%` } })),
          h('div', { class: 'muted small mt' }, `${c.available_for_regular_users} slot(s) free for regular users · ${c.reserved_connections} reserved · ${c.walsenders} walsender(s)`)] : h('div', { class: 'muted' }, 'Not collected yet')),
        h('div', { class: 'card' }, h('h2', 'By state'), bars((c.by_state || []).map(s => ({ label: s.state || 'unknown', value: s.sessions })))),
        h('div', { class: 'card' }, h('h2', 'Top applications'), bars((c.by_application || []).slice(0, 5).map(s => ({ label: s.name, value: s.sessions }))))), h('div', { class: 'mt' }, metaLine(d.meta)),
        d.truncated ? callout('warn', 'The session list was capped at 1500 rows by the collector; filters apply to the captured rows.') : null);
      const cols = [
        { key: 'pid', label: 'PID', sortable: true, render: r => h('span', { class: 'mono' }, r.pid) }, { key: 'state', label: 'State', sortable: true, render: r => badge(r.state || 'unknown', r.state === 'active' ? 'active' : (r.state || '').startsWith('idle in') ? 'warning' : 'neutral') },
        { key: 'who', label: 'Application / user', render: r => h('div', h('div', r.application_name || h('span', { class: 'muted' }, 'no application_name')), h('div', { class: 'muted small' }, `${r.usename || '?'}@${r.datname || '?'}`)) },
        { key: 'wait', label: 'Wait', render: r => r.wait_event_type ? h('span', { class: 'small' }, `${r.wait_event_type}:${r.wait_event}`) : h('span', { class: 'muted' }, '–') },
        { key: 'xact_age_s', label: 'Xact age', sortable: true, num: true, render: r => dur(r.xact_age_s) }, { key: 'query_age_s', label: 'Query age', sortable: true, num: true, render: r => dur(r.query_age_s) },
        { key: 'query', label: 'Query', render: r => h('span', { class: 'qtext', title: r.query || '' }, r.query || '–') }];
      tableBox.replaceChildren(dataTable({ columns: cols, rows: d.items, sort: { key: f.sort, dir: f.order }, onSort: k => { f.order = f.sort === k && f.order === 'desc' ? 'asc' : 'desc'; f.sort = k; load(); }, caption: 'Sessions',
        expand: r => h('div', h('div', { class: 'row' }, copyBtn(r.query || '', 'Copy query'), r.blocked_by.length ? badge(`blocked by ${r.blocked_by.join(', ')}`, 'critical') : null, r.query_fingerprint ? h('a', { class: 'btn small', href: `#/queries?fingerprint=${r.query_fingerprint}` }, 'Query trend') : null),
          h('pre', { class: 'json mt' }, r.query || '(no query text)'), kv([['Client', r.client_addr || '–'], ['Backend start', r.backend_start], ['Transaction start', r.xact_start || '–'], ['Query start', r.query_start || '–'], ['State change', r.state_change || '–'], ['xmin age', r.xmin_age != null ? num(r.xmin_age) : '–']])),
        empty: empty('No sessions match', 'Adjust the filters, or the recorder has not collected yet.') }), pager({ page: d.page, pages: d.pages, total: d.total, pageSize: d.page_size, onPage: p => { f.page = p; load(); } }));
    } catch (e) { top.replaceChildren(); tableBox.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = load; await load(); return root;
}
