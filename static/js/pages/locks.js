// Locks and Blocking: current graph (root vs direct blockers) and episodes captured while they happened.
import { get, inst } from '../api.js';
import { h, badge, sev, dur, num, timeEl, metaLine, errorBox, empty, callout, dataTable, kv, copyBtn, tabs } from '../ui.js';
import { pageHead, refreshButton, goto } from '../common.js';
import { blockingGraph } from '../graph.js';
import { openEvidence } from './evidence.js';

export async function render(ctx) {
  const root = h('div'); const graphBox = h('div'); const detailBox = h('div'); const episodes = h('div'); let selected = null; let last = null;
  const help = h('details', { class: 'small muted', style: { marginTop: '8px' } }, h('summary', 'Definitions'), h('ul'));
  root.append(pageHead('Locks and Blocking', 'Who is blocking whom right now, and what the recorder captured while it happened.', refreshButton(load)), callout('', h('strong', 'How to read this. '), 'Lock inspection reflects the instant of collection. Historical blocking exists only where the recorder captured it (see Evidence).'),
    h('div', { class: 'card mt' }, h('div', { class: 'card-head' }, h('h2', 'Blocking graph (current)'), h('span', { id: 'lockmeta' })), graphBox, detailBox), h('div', { class: 'mt' }, episodes), help);
  function showNode(n) {
    selected = n.pid; drawGraph();
    const holds = (n.holds || []).map(l => `${l.locktype}${l.relation_name ? ' ' + l.relation_name : ''} (${l.mode})`); const waits = (n.waiting_for || []).map(l => `${l.locktype}${l.relation_name ? ' ' + l.relation_name : ''} (${l.mode})`);
    detailBox.replaceChildren(h('div', { class: 'card mt', style: { background: 'var(--panel2)' } }, h('div', { class: 'row' }, h('h3', `PID ${n.pid}`), badge(n.role.replace('_', ' '), n.role === 'root_blocker' ? 'critical' : n.role === 'intermediate' ? 'warning' : 'neutral'), h('span', { class: 'spacer' }), copyBtn(String(n.pid), 'Copy PID')),
      kv([['Application', n.application_name || '–'], ['User / database', `${n.usename || '?'} / ${n.datname || '?'}`], ['State', n.state], ['Transaction age', dur(n.xact_age_s)], ['Statement age', dur(n.query_age_s)],
        n.direct_blockers && n.direct_blockers.length ? ['Directly blocked by', n.direct_blockers.join(', ')] : null, n.directly_blocks && n.directly_blocks.length ? ['Directly blocks', n.directly_blocks.slice(0, 15).join(', ') + (n.directly_blocks.length > 15 ? '…' : '')] : null,
        n.blocked_count != null ? ['Blocks in total', `${n.blocked_count} session(s), chain depth ${n.depth}`] : null, holds.length ? ['Locks held', holds.join('; ')] : null, waits.length ? ['Waiting for', waits.join('; ')] : null]),
      h('div', { class: 'mt small muted' }, 'Statement'), h('pre', { class: 'json' }, n.query || '(no text: hidden by configuration or unavailable)')));
  }
  function drawGraph() { if (!last) return; graphBox.replaceChildren(last.graph.nodes.length ? blockingGraph(last.graph, { onSelect: showNode, selected }) : empty('No blocking right now', 'No session is waiting for a lock at the last collection. Past events are listed below.')); }
  async function load() {
    try {
      last = await get('/api/live/blocking', inst());
      root.querySelector('#lockmeta').replaceChildren(metaLine(last.meta) || '');
      if (last.graph.cycles.length) graphBox.before(callout('crit', `Lock wait cycle detected between PIDs ${last.graph.cycles[0].join(', ')}.`));
      drawGraph();
      if (selected && !last.graph.nodes.some(n => n.pid === selected)) { selected = null; detailBox.replaceChildren(); }
      help.querySelector('ul').replaceChildren(...Object.entries(last.definitions).map(([k, v]) => h('li', h('strong', k.replace('_', ' ') + ': '), v)));
      episodes.replaceChildren(h('div', { class: 'card-head' }, h('h2', 'Captured blocking episodes'), h('span', { class: 'muted' }, 'Recorded while they happened')),
        dataTable({ columns: [{ key: 's', label: 'Severity', render: e => sev(e.severity) }, { key: 't', label: 'Episode', render: e => e.title }, { key: 'f', label: 'First seen', render: e => timeEl(e.first_seen) },
          { key: 'd', label: 'Duration', render: e => e.status === 'active' ? badge('ongoing', 'active') : dur(e.duration_s) }, { key: 'p', label: 'Peak', render: e => e.peaks && e.peaks.blocked_count ? `${e.peaks.blocked_count} blocked, wait ≥ ${dur(e.peaks.max_wait_s)}` : '–' }],
          rows: last.recent_evidence, onRow: e => { history.replaceState(null, '', `#/evidence/${e.id}`); openEvidence(e.id, ctx); }, empty: empty('Nothing captured', 'No blocking episode has been recorded for this instance.'), caption: 'Blocking episodes' }));
    } catch (e) { graphBox.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = load; await load();
  const pid = Number(ctx.qs.get('pid')); if (pid && last) { const n = last.graph.nodes.find(x => x.pid === pid); if (n) showNode(n); }
  return root;
}
