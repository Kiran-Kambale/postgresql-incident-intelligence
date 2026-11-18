// Replication and HA: standbys with lag decomposition, slots (physical vs logical), receiver/recovery, and the optional Patroni boundary.
import { get, inst } from '../api.js';
import { range } from '../state.js';
import { h, badge, dur, num, bytes, dataTable, metaLine, errorBox, empty, callout, tabs, kv, timeEl } from '../ui.js';
import { pageHead, refreshButton } from '../common.js';
import { lineChart } from '../charts.js';

export async function render(ctx) {
  const root = h('div'); const body = h('div'); let view = ctx.qs.get('view') || 'standbys';
  root.append(pageHead('Replication and HA', 'Streaming standbys, replication slots and recovery state. Cluster-manager state is shown only when an integration is configured.', refreshButton(load)), body);
  async function load() {
    const r = range();
    try {
      const [d, m] = await Promise.all([get('/api/live/replication', inst()), get('/api/metrics/series', inst({ names: 'repl_max_replay_lag_s,slot_max_retained_bytes', from: r.from, to: r.to })).catch(() => null)]);
      if (d.meta.status === 'pending') { body.replaceChildren(empty('Waiting for the first collection', 'Replication state is collected every minute by default.')); return; }
      const role = d.role === 'standby' ? 'standby (in recovery)' : 'primary';
      const standbys = () => h('div', d.role === 'standby' ? callout('', 'This server is a standby. Its own replay and receiver state is below; downstream standbys (if any) are listed after.') : null,
        d.standbys.length ? dataTable({ caption: 'Standbys', columns: [{ key: 'n', label: 'Standby', render: s => s.application_name || s.client_addr || `pid ${s.pid}` }, { key: 'st', label: 'State', render: s => badge(s.state, s.state === 'streaming' ? 'ok' : 'warning') }, { key: 'sy', label: 'Sync', render: s => s.sync_state },
          { key: 'w', label: 'Write lag', num: true, tip: 'Time for the standby to write WAL (network + OS write)', render: s => s.write_lag_s != null ? dur(s.write_lag_s) : '–' }, { key: 'f', label: 'Flush lag', num: true, tip: 'Time until WAL was flushed to disk on the standby', render: s => s.flush_lag_s != null ? dur(s.flush_lag_s) : '–' },
          { key: 'r', label: 'Replay lag', num: true, tip: 'Time until changes became visible on the standby', render: s => s.replay_lag_s != null ? dur(s.replay_lag_s) : '–' }, { key: 'b', label: 'Behind', num: true, render: s => bytes(s.total_lag_bytes) }], rows: d.standbys }) : empty(d.role === 'standby' ? 'No downstream standbys' : 'No standbys connected', 'pg_stat_replication has no rows.'),
        h('p', { class: 'muted small' }, 'Lag values come from PostgreSQL acknowledgements and read 0 or empty when the primary is idle. The lag stages show where the delay is visible, not why it happens.'),
        m ? h('div', { class: 'card mt' }, h('h2', 'Max replay lag over time'), lineChart({ from: r.from, to: r.to, gaps: m.gaps, step: m.step_s, series: [{ name: 'Replay lag (s)', points: m.series.repl_max_replay_lag_s.points }], fmt: v => dur(v), thresholds: [{ value: 30, label: 'warn 30s' }] })) : null);
      const slots = () => h('div', d.slots.length ? dataTable({ caption: 'Replication slots', columns: [{ key: 'n', label: 'Slot', render: s => s.slot_name }, { key: 't', label: 'Kind', render: s => badge(s.slot_type, 'neutral') }, { key: 'a', label: 'Active', render: s => badge(s.active ? 'active' : 'inactive', s.active ? 'ok' : 'warning') },
        { key: 'ret', label: 'WAL retained', num: true, render: s => bytes(s.retained_bytes) }, { key: 'ws', label: 'wal_status', render: s => s.wal_status || h('span', { class: 'muted' }, 'n/a (PG < 13)') }, { key: 'x', label: 'xmin / catalog xmin age', num: true, render: s => `${num(s.xmin_age)} / ${num(s.catalog_xmin_age)}` }, { key: 'db', label: 'Database', render: s => s.database || '–' }], rows: d.slots }) : empty('No replication slots', 'pg_replication_slots is empty.'),
        h('p', { class: 'muted small' }, `max_slot_wal_keep_size = ${d.settings.max_slot_wal_keep_size}. Logical slots (CDC, publications) retain WAL until their consumer confirms it, independent of physical standbys.`),
        m ? h('div', { class: 'card mt' }, h('h2', 'Largest WAL retained by a slot'), lineChart({ from: r.from, to: r.to, gaps: m.gaps, step: m.step_s, series: [{ name: 'Retained bytes', points: m.series.slot_max_retained_bytes.points }], fmt: v => bytes(v), area: true })) : null);
      const standbyState = () => d.in_recovery ? h('div', kv([['Receiver status', d.receiver ? d.receiver.status : 'no WAL receiver'], ['Received LSN', d.receiver ? d.receiver.received_lsn : '–'], ['Last replay timestamp', d.recovery ? timeEl(d.recovery.last_replay_ts) : '–'], ['Replay delay', d.recovery ? dur(d.recovery.replay_delay_s) : '–'], ['Replay paused', d.recovery ? String(d.recovery.replay_paused) : '–'], ['Receive vs replay gap', d.recovery ? bytes(d.recovery.receive_replay_gap_bytes) : '–']]),
        callout('warn', 'On a quiet primary the replay delay grows without any real lag; compare with the receive/replay LSN gap.')) : empty('Not a standby', 'Recovery state applies only to standby servers. This server is a primary.');
      const logical = () => d.logical_subscriptions.length ? dataTable({ caption: 'Logical subscriptions', columns: [{ key: 'subname', label: 'Subscription' }, { key: 'pid', label: 'Worker PID' }, { key: 'received_lsn', label: 'Received LSN' }, { key: 'latest_end_time', label: 'Latest end', render: s => s.latest_end_time ? timeEl(s.latest_end_time) : '–' }], rows: d.logical_subscriptions }) : empty('No subscriptions on this server', 'Logical replication publications are shown as logical slots; subscriptions appear on the subscriber.');
      const ha = d.integrations.patroni;
      const haPanel = () => ha.status === 'not_configured' ? callout('', h('strong', 'Cluster manager state is not available. '), ha.note) : h('div', callout(ha.status === 'ok' ? '' : 'warn', `Patroni REST API: ${ha.status}. ${ha.note || ''}`), ha.members ? dataTable({ caption: 'Cluster members', columns: [{ key: 'name', label: 'Member' }, { key: 'role', label: 'Role' }, { key: 'state', label: 'State' }, { key: 'lag', label: 'Lag' }, { key: 'timeline', label: 'Timeline' }], rows: ha.members }) : null);
      body.replaceChildren(h('div', { class: 'row small' }, badge(role, 'info'), metaLine(d.meta)), h('div', { class: 'mt' }, tabs([{ id: 'standbys', label: 'Standbys and lag', render: standbys }, { id: 'slots', label: 'Slots and WAL retention', render: slots }, { id: 'recovery', label: 'Standby recovery', render: standbyState }, { id: 'logical', label: 'Logical replication', render: logical }, { id: 'ha', label: 'Cluster manager', render: haPanel }], view, v => { view = v; })));
    } catch (e) { body.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = load; await load(); return root;
}
