// Database Instances: registry, connection test, privileges and the capability matrix (what is available vs why not).
import { get, post, inst } from '../api.js';
import { state } from '../state.js';
import { h, badge, timeEl, dur, dataTable, errorBox, empty, callout, toast, kv, tabs, confirmDialog } from '../ui.js';
import { pageHead, refreshButton } from '../common.js';

export async function render(ctx) {
  const root = h('div'); const body = h('div');
  root.append(pageHead('Database Instances', 'Monitored PostgreSQL instances, how each is reached, and what the monitoring role can and cannot see.', refreshButton(load)), body);
  async function test(i, out) {
    try { const r = await post(`/api/instances/${i.id}/test`); out.replaceChildren(r.ok ? (r.demo ? callout('', r.message) : callout('', h('strong', `Connected: ${r.version}`), h('div', `Role ${r.current_user} · supported: ${r.supported} · pg_stat_statements: ${r.pg_stat_statements ? 'installed' : 'not installed'}`), h('div', `Privileges: ${Object.entries(r.privileges).map(([k, v]) => `${k}=${v}`).join(', ')}`), h('div', `TLS: ${r.tls.in_use ? 'in use (' + r.tls.version + ')' : 'not in use'}, certificate verified: ${r.tls.certificate_verified}`), ...(r.warnings || []).map(w => callout('warn', w)))) : callout('crit', h('strong', `${r.error_code}: `), r.message, r.hint ? h('div', r.hint) : '')); } catch (e) { out.replaceChildren(errorBox(e)); }
  }
  async function load() {
    try {
      const [d, caps] = await Promise.all([get('/api/instances'), get('/api/system/capabilities', inst())]);
      const cards = d.items.map(i => { const out = h('div', { class: 'mt' }); return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', i.name), h('span', { class: 'row' }, badge(i.demo ? 'demo' : i.enabled ? 'monitoring' : 'paused', i.demo || i.enabled ? 'ok' : 'warning'), i.role ? badge(i.role, 'info') : null)),
        kv([['Id', i.id], ['Endpoint', i.demo ? 'synthetic (no connection)' : `${i.host}:${i.port}/${i.dbname}`], ['User', i.user], ['Version', i.version || 'not collected yet'], ['Environment', i.environment || '–'], ['TLS mode', `${i.sslmode}${i.tls_verified ? ' (certificate verified)' : ' (certificate NOT verified)'}`], ['Server log', i.log_file_configured ? 'configured' : 'not configured']]),
        h('div', { class: 'small mt' }, Object.entries(i.tiers).map(([t, s]) => h('div', { class: 'row' }, h('span', { style: { width: '70px' } }, t), badge(s.last_status || 'pending', s.last_status === 'ok' ? 'ok' : s.last_status ? 'warning' : 'neutral'), h('span', { class: 'muted' }, s.last_run_at ? `last ${dur((Date.now() - Date.parse(s.last_run_at)) / 1000)} ago` : '–', s.consecutive_failures ? ` · ${s.consecutive_failures} consecutive failure(s)` : '')))),
        h('div', { class: 'row mt' }, h('button', { class: 'btn small', type: 'button', onclick: () => test(i, out) }, 'Test connection'), h('button', { class: 'btn small', type: 'button', onclick: async () => { if (await confirmDialog({ title: i.enabled ? 'Pause monitoring?' : 'Resume monitoring?', body: i.enabled ? 'Collection stops until resumed or the server restarts. The timeline will show a gap.' : 'Collection resumes immediately.', confirmText: i.enabled ? 'Pause' : 'Resume' })) { try { await post(`/api/instances/${i.id}/monitoring`, { enabled: !i.enabled }); load(); } catch (e) { toast(e.message, 'err'); } } } }, i.enabled ? 'Pause monitoring' : 'Resume monitoring')), out); });
      const capTable = dataTable({ caption: 'Capability matrix', columns: [{ key: 'c', label: 'Capability', render: c => h('div', h('strong', c.capability), h('div', { class: 'muted small' }, c.purpose)) }, { key: 's', label: 'Status', render: c => badge(c.status, c.status.startsWith('ok') ? 'ok' : c.status === 'not_configured' || c.status === 'not_collected_yet' ? 'neutral' : 'warning') },
        { key: 'd', label: 'Detail', render: c => c.detail ? h('span', { class: 'small' }, c.detail) : h('span', { class: 'muted' }, '–') }, { key: 'r', label: 'Needs', render: c => h('span', { class: 'small muted' }, c.requires) }], rows: caps.capabilities });
      body.replaceChildren(...(d.warnings || []).map(w => callout('warn', w)), h('div', { class: 'grid g2' }, cards), h('div', { class: 'card mt' }, h('h2', 'What the monitoring role can see'), caps.server ? kv([['Role', caps.server.current_user], ['Superuser', String(caps.server.is_superuser)], ['pg_monitor', String(caps.server.pg_monitor)], ['pg_read_all_stats', String(caps.server.pg_read_all_stats)], ['pg_read_all_settings', String(caps.server.pg_read_all_settings)], ['TLS in use', caps.server.ssl_in_use ? `yes (${caps.server.ssl_version})` : 'no']]) : h('p', { class: 'muted' }, 'Not collected yet.'),
        caps.server && caps.server.is_superuser ? callout('warn', 'The monitoring role is a superuser. Use a least-privilege role (docs/permissions.md).') : null, h('div', { class: 'mt' }, capTable), h('p', { class: 'muted small' }, `Supported versions: ${caps.supported_versions}. Cluster manager: ${caps.integrations.patroni.status === 'not_configured' ? 'not configured (SQL cannot show Patroni/repmgr state)' : caps.integrations.patroni.status}.`)));
    } catch (e) { body.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = null; await load(); return root;
}
