// Transactions and Vacuum: XID age, vacuum pressure with context, xmin holders, running maintenance, growth.
import { get, inst } from '../api.js';
import { range } from '../state.js';
import { h, badge, dur, num, bytes, dataTable, metaLine, errorBox, empty, callout, tabs, kv, sev, timeEl } from '../ui.js';
import { pageHead, refreshButton, stepFor } from '../common.js';
import { lineChart, bars } from '../charts.js';
import { openEvidence } from './evidence.js';

export async function render(ctx) {
  const root = h('div'); const body = h('div'); let view = ctx.qs.get('view') || 'pressure';
  root.append(pageHead('Transactions and Vacuum', 'Transaction ID age, dead tuples, what holds back vacuum, and maintenance in progress.', refreshButton(load)), body);
  async function load() {
    const r = range();
    try {
      const [d, m] = await Promise.all([get('/api/live/vacuum', inst()), get('/api/metrics/series', inst({ names: 'xid_age_max,oldest_xmin_age,dead_tuples_max', from: r.from, to: r.to })).catch(() => null)]);
      if (d.meta.status === 'pending') { body.replaceChildren(empty('Waiting for the first collection', 'Vacuum diagnostics are collected every few minutes.')); return; }
      const xid = (d.databases_xid || [])[0]; const horizon = d.xmin_holders || { backends: [], slots: [], prepared: [] };
      const pressure = () => h('div', dataTable({ caption: 'Tables ranked by dead tuples', columns: [
        { key: 'rel', label: 'Table', render: t => `${t.schemaname}.${t.relname}` }, { key: 'dead', label: 'Dead tuples', num: true, render: t => num(t.n_dead_tup) }, { key: 'ratio', label: 'Dead %', num: true, render: t => `${t.dead_ratio_pct}%` },
        { key: 'live', label: 'Live tuples', num: true, render: t => num(t.n_live_tup) }, { key: 'size', label: 'Size', num: true, render: t => bytes(t.total_bytes) },
        { key: 'av', label: 'Last autovacuum', render: t => t.last_autovacuum ? timeEl(t.last_autovacuum) : h('span', { class: 'muted' }, t.last_vacuum ? 'manual only' : 'not since stats reset') }, { key: 'n', label: 'Runs', num: true, render: t => num(t.autovacuum_count) },
        { key: 'o', label: 'Table options', render: t => t.reloptions ? h('span', { class: 'small mono' }, t.reloptions) : h('span', { class: 'muted' }, 'defaults') }], rows: d.tables, empty: empty('No tables to show', 'No user tables, or statistics are not available.') }),
        h('p', { class: 'muted small' }, d.note));
      const horizonPanel = () => h('div', callout('', 'Vacuum cannot remove row versions newer than the oldest xmin. Holders are listed oldest first. A holder is not necessarily a problem: judge by its age and what it is.'),
        h('div', { class: 'grid g3 mt' }, h('div', { class: 'card' }, h('h3', 'Sessions'), horizon.backends.length ? horizon.backends.map(b => h('div', { class: 'small', style: { margin: '6px 0' } }, `PID ${b.pid} · ${b.application_name || b.usename} · ${b.state} · age ${num(b.xmin_age)}`)) : h('div', { class: 'muted' }, 'None holding an xmin')),
          h('div', { class: 'card' }, h('h3', 'Replication slots'), horizon.slots.length ? horizon.slots.map(s => h('div', { class: 'small', style: { margin: '6px 0' } }, `${s.slot_name} (${s.slot_type}${s.active ? '' : ', inactive'}) · xmin age ${num(s.xmin_age)} · catalog xmin age ${num(s.catalog_xmin_age)}`)) : h('div', { class: 'muted' }, 'None')),
          h('div', { class: 'card' }, h('h3', 'Prepared transactions'), d.prepared_xacts.length ? d.prepared_xacts.map(p => h('div', { class: 'small', style: { margin: '6px 0' } }, `${p.gid} · owner ${p.owner} · prepared ${p.prepared}`)) : h('div', { class: 'muted' }, 'None'))));
      const xidPanel = () => h('div', h('div', { class: 'grid g3' }, (d.databases_xid || []).map(x => h('div', { class: 'card' }, h('h3', x.datname), h('div', { style: { fontSize: '22px', fontWeight: 700 } }, num(x.xid_age)), h('div', { class: 'bar' + (x.xid_age >= 1.5e9 ? ' crit' : x.xid_age >= 1e9 ? ' warn' : '') }, h('i', { style: { width: `${100 * x.xid_age / 2147483648}%` } })),
        h('div', { class: 'muted small' }, `${(100 * x.xid_age / 2147483648).toFixed(1)}% of the ~2.1 billion wraparound limit · multixact age ${num(x.mxid_age)}`)))),
        m ? h('div', { class: 'card mt' }, h('h2', 'XID age over time'), lineChart({ from: r.from, to: r.to, gaps: m.gaps, step: m.step_s, series: [{ name: 'Max database XID age', points: m.series.xid_age_max.points }, { name: 'Oldest xmin age', points: m.series.oldest_xmin_age.points }], fmt: v => num(v, 0), thresholds: [{ value: 1e9, label: 'warn 1.0e9' }] })) : null,
        h('p', { class: 'muted small' }, 'The rise rate (not just the level) shows how long until freezing must complete. Settings: ' + Object.entries(d.settings || {}).map(([k, v]) => `${k}=${v}`).join(', ')));
      const maint = () => h('div', d.vacuum_progress.length ? dataTable({ caption: 'Running vacuum', columns: [{ key: 'pid', label: 'PID' }, { key: 'relation', label: 'Table' }, { key: 'phase', label: 'Phase' }, { key: 'prog', label: 'Heap progress', render: p => p.heap_blks_total ? `${(100 * p.heap_blks_scanned / p.heap_blks_total).toFixed(0)}%` : '–' }, { key: 'run', label: 'Running', render: p => dur(p.running_s) }], rows: d.vacuum_progress }) : empty('No vacuum running', 'No VACUUM or autovacuum worker was running at the last collection.'),
        h('div', { class: 'grid g2 mt' }, h('div', { class: 'card' }, h('h3', 'CREATE INDEX / REINDEX'), d.maintenance.create_index.length ? d.maintenance.create_index.map(x => h('div', { class: 'small' }, `PID ${x.pid} · ${x.command} · ${x.phase} · ${dur(x.running_s)}`)) : h('div', { class: 'muted' }, 'None running')),
          h('div', { class: 'card' }, h('h3', 'CLUSTER / VACUUM FULL'), d.maintenance.cluster.length ? d.maintenance.cluster.map(x => h('div', { class: 'small' }, `PID ${x.pid} · ${x.command} · ${x.phase} · ${dur(x.running_s)}`)) : h('div', { class: 'muted' }, 'None running'))));
      const growth = () => d.growth_7d.length ? dataTable({ caption: 'Table growth', columns: [{ key: 'relation', label: 'Table' }, { key: 'last', label: 'Size now', num: true, render: g => bytes(g.last_bytes) }, { key: 'g', label: 'Growth in window', num: true, render: g => bytes(g.growth_bytes) }, { key: 'pd', label: 'Per day', num: true, render: g => g.growth_per_day_bytes != null ? bytes(g.growth_per_day_bytes) : '–' }, { key: 's', label: 'Samples', num: true, render: g => g.samples }], rows: d.growth_7d }) : empty('Not enough samples yet', d.growth_note);
      body.replaceChildren(h('div', { class: 'row small' }, metaLine(d.meta)), tabs([{ id: 'pressure', label: 'Vacuum pressure', render: pressure }, { id: 'xid', label: 'XID age', render: xidPanel }, { id: 'horizon', label: 'What holds back vacuum', render: horizonPanel }, { id: 'running', label: 'Running maintenance', render: maint }, { id: 'growth', label: 'Growth', render: growth }], view, v => { view = v; }));
    } catch (e) { body.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = load; await load(); return root;
}
