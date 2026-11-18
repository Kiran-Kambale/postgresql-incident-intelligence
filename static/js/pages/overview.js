// Overview: what is happening now, what the recorder has seen, where to look next.
import { get, post, inst } from '../api.js';
import { state, range, set } from '../state.js';
import { h, icon, badge, sev, kpi, dur, num, bytes, timeEl, metaLine, skeleton, errorBox, empty, toast, callout } from '../ui.js';
import { pageHead, refreshButton, findingRow, goto, stepFor } from '../common.js';
import { lineChart } from '../charts.js';
import { openEvidence } from './evidence.js';

export async function render(ctx) {
  const root = h('div'); const kpis = h('div'); const body = h('div');
  const refreshBtn = refreshButton(async () => { try { await post('/api/live/refresh', { tiers: ['fast', 'standard'] }, inst()); toast('Collected', 'ok'); } catch (e) { toast(e.message, 'err'); } load(); });
  root.append(pageHead('Overview', 'What is happening now, what the flight recorder has seen, and where to look next.', refreshBtn), kpis, body);
  async function load() {
    const r = range();
    try {
      const [o, m] = await Promise.all([get('/api/live/overview', inst()),
        get('/api/metrics/series', inst({ names: 'sessions_total,sessions_active,blocked_sessions,conn_utilization_pct', from: r.from, to: r.to, max_points: 240 })).catch(() => null)]);
      drawKpis(o); drawBody(o, m, r);
    } catch (e) { kpis.replaceChildren(); body.replaceChildren(errorBox(e, load)); }
  }
  function drawKpis(o) {
    const k = o.kpis; const na = k.sessions_total == null;
    const tone = (v, w, c) => v == null ? '' : v >= c ? 'crit' : v >= w ? 'warn' : '';
    kpis.replaceChildren(h('div', { class: 'grid g-kpi' },
      kpi({ label: 'Sessions', value: `${num(k.sessions_active)} / ${num(k.sessions_total)}`, hint: 'active / total client sessions', na, onclick: () => goto('#/activity') }),
      kpi({ label: 'Blocked sessions', tipText: 'Sessions waiting for a lock held by another session (from pg_blocking_pids at collection time).', value: num(k.blocked_sessions), hint: `${num(k.root_blockers)} root blocker(s)`, tone: k.blocked_sessions > 0 ? (k.blocked_sessions >= 5 ? 'crit' : 'warn') : '', na, onclick: () => goto('#/locks') }),
      kpi({ label: 'Idle in transaction', value: num(k.sessions_idle_in_txn), hint: 'holding locks and snapshots', tone: k.sessions_idle_in_txn > 0 ? 'warn' : '', na, onclick: () => goto('#/activity?state=idle in transaction') }),
      kpi({ label: 'Connection use', value: k.utilization_percent != null ? `${k.utilization_percent}%` : '–', hint: `of ${num(k.max_connections)} max_connections`, tone: tone(k.utilization_percent, 80, 90), bar: k.utilization_percent, na, onclick: () => goto('#/activity') }),
      kpi({ label: 'Longest query', value: dur(k.longest_query_s), hint: `longest transaction ${dur(k.longest_xact_s)}`, tone: k.longest_query_s >= 30 ? 'warn' : '', na, onclick: () => goto('#/activity?sort=query_age_s') }),
      kpi({ label: 'Replication', value: k.standbys != null ? `${k.standbys} standby` : '–', hint: k.standbys != null ? `max lag ${dur(k.max_replay_lag_s)} · ${k.inactive_slots} inactive slot(s)` : 'not collected yet', tone: k.inactive_slots > 0 || k.max_replay_lag_s >= 30 ? 'warn' : '', na: k.standbys == null, onclick: () => goto('#/replication') }),
      kpi({ label: 'XID age', value: k.xid_age_max != null ? num(k.xid_age_max) : '–', hint: k.xid_age_max != null ? `${(100 * k.xid_age_max / 2147483648).toFixed(0)}% of wraparound limit` : 'collected every few minutes', tone: tone(k.xid_age_max, 1e9, 1.5e9), na: k.xid_age_max == null, onclick: () => goto('#/vacuum') }),
      kpi({ label: 'Active findings', value: num(o.evidence.counts.active), hint: `${o.evidence.counts.active_by_severity.critical || 0} critical · ${o.evidence.counts.active_by_severity.warning || 0} warning`, tone: (o.evidence.counts.active_by_severity.critical || 0) ? 'crit' : o.evidence.counts.active ? 'warn' : '', onclick: () => goto('#/evidence?status=active') })));
  }
  function drawBody(o, m, r) {
    const mon = o.monitoring;
    const monCard = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Monitoring status'), badge(mon.status, mon.status === 'healthy' || mon.status === 'demo' ? 'ok' : mon.status === 'degraded' ? 'warning' : 'neutral')),
      h('div', { class: 'small' }, `Coverage, last hour: ${mon.coverage_1h_pct}% · gaps: ${mon.gaps_1h}`), h('div', { class: 'muted small' }, o.server && o.server.version_string ? o.server.version_string : ''),
      h('div', { class: 'mt small' }, Object.entries(mon.tiers).map(([t, s]) => h('div', { class: 'row', style: { marginBottom: '2px' } }, h('span', { style: { width: '70px' } }, t), badge(s.last_status || 'pending', s.last_status === 'ok' ? 'ok' : s.last_status ? 'warning' : 'neutral'),
        h('span', { class: 'muted' }, s.last_run_at ? `${dur((Date.now() - Date.parse(s.last_run_at)) / 1000)} ago · ${s.duration_ms} ms` : 'not run yet')))),
      o.failures.length ? h('div', { class: 'mt' }, o.failures.map(f => callout('crit', h('strong', `${f.collector}: `), f.message || f.error_code))) : null, h('div', { class: 'mt' }, metaLine(o.meta)));
    const findings = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Active findings'), h('a', { href: '#/evidence?status=active' }, 'All evidence')),
      o.evidence.active.length ? o.evidence.active.map(e => findingRow(e, x => { history.replaceState(null, '', `#/evidence/${x.id}`); openEvidence(x.id, ctx); })) : empty('Nothing needs attention', 'No active warning or critical evidence at the last collection.'));
    const incidents = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Incidents needing attention'), h('a', { href: '#/incidents' }, 'All incidents')),
      o.incidents.open.length ? o.incidents.open.map(i => h('div', { class: 'card', style: { padding: '8px 12px', marginBottom: '8px', cursor: 'pointer' }, tabindex: 0, role: 'link', onclick: () => goto(`#/incidents/${i.id}`), onkeydown: e => { if (e.key === 'Enter') { e.preventDefault(); goto(`#/incidents/${i.id}`); } } },
        h('div', { class: 'row' }, badge(i.status, i.status), badge(i.severity, i.severity), h('strong', i.title)), h('div', { class: 'muted small' }, `${i.id} · stage ${i.stage} · ${i.evidence_count} evidence`))) : empty('No open incidents', 'Candidates appear here when critical evidence is detected.'));
    let charts;
    if (m) {
      const S = n => (m.series[n] || { points: [] }).points;
      charts = h('div', { class: 'grid g2' },
        h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Sessions'), h('span', { class: 'muted' }, `coverage ${m.coverage_pct}%`)),
          lineChart({ from: r.from, to: r.to, gaps: m.gaps, step: m.step_s, series: [{ name: 'Total', points: S('sessions_total') }, { name: 'Active', points: S('sessions_active') }, { name: 'Blocked', points: S('blocked_sessions') }], fmt: v => num(v, 0),
            onBrush: (a, b) => set(a ? { rangePreset: 'custom', customRange: { from: a, to: b } } : { rangePreset: '1h', customRange: null }) })),
        h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Connection utilization'), h('span', { class: 'muted' }, '% of max_connections')),
          lineChart({ from: r.from, to: r.to, gaps: m.gaps, step: m.step_s, series: [{ name: 'Utilization %', points: S('conn_utilization_pct') }], fmt: v => `${num(v, 0)}%`, thresholds: [{ value: 80, label: 'warn 80%' }], area: true })));
    } else charts = empty('No time series yet', 'Charts appear once the recorder has stored samples.');
    body.replaceChildren(h('div', { class: 'grid g2 mt' }, findings, h('div', { class: 'grid' }, monCard, incidents)), h('div', { class: 'mt' }, charts));
  }
  ctx.tick = load; await load(); return root;
}
