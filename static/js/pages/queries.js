// Query Performance from pg_stat_statements samples: windowed top statements, trends and regression hypotheses.
import { get, inst } from '../api.js';
import { state, range } from '../state.js';
import { h, badge, ms, num, dur, dataTable, metaLine, errorBox, empty, callout, limitations, copyBtn, kv, sev, skeleton } from '../ui.js';
import { pageHead, refreshButton } from '../common.js';
import { lineChart } from '../charts.js';
import { openEvidence } from './evidence.js';

export async function render(ctx) {
  const f = { window: 60, sort: 'total' }; const root = h('div'); const body = h('div');
  const winSel = h('select', { 'aria-label': 'Window', onchange: () => { f.window = Number(winSel.value); load(); } }, [[15, 'Last 15 min'], [60, 'Last hour'], [360, 'Last 6 hours'], [1440, 'Last 24 hours']].map(([v, l]) => h('option', { value: v, selected: v === f.window }, l)));
  const sortSel = h('select', { 'aria-label': 'Sort by', onchange: () => { f.sort = sortSel.value; load(); } }, [['total', 'Sort: total time'], ['mean', 'Sort: mean time'], ['calls', 'Sort: calls'], ['rows', 'Sort: rows'], ['temp', 'Sort: temp blocks']].map(([v, l]) => h('option', { value: v }, l)));
  root.append(pageHead('Query Performance', 'Per-statement time between stored samples of pg_stat_statements. Window values are deltas, not single executions.', [winSel, sortSel, refreshButton(load)]), body);
  async function trend(item) {
    const r = range(); const box = h('div', skeleton(1, 160));
    try {
      const t = await get('/api/queries/trend', inst({ queryid: item.queryid, from: new Date(Date.now() - Math.max(f.window, 360) * 60000).toISOString(), to: new Date().toISOString() }));
      const from = new Date(Date.now() - Math.max(f.window, 360) * 60000).toISOString(), to = new Date().toISOString();
      box.replaceChildren(h('div', { class: 'small muted' }, t.note), lineChart({ from, to, step: 600, series: [{ name: 'mean ms / call', points: t.points.map(p => ({ ts: p.ts, value: p.mean_ms })) }], fmt: v => ms(v), height: 150, empty: 'Not enough samples yet for a trend.' }),
        lineChart({ from, to, step: 600, series: [{ name: 'calls per interval', points: t.points.map(p => ({ ts: p.ts, value: p.calls })), color: '#3fb68b' }], fmt: v => num(v, 0), height: 120 }));
    } catch (e) { box.replaceChildren(errorBox(e)); }
    return box;
  }
  async function load() {
    try {
      const d = await get('/api/queries/top', inst({ window_min: f.window, sort: f.sort, limit: 25 }));
      const notReady = d.meta.status !== 'ok' && !d.items.length;
      if (notReady && ['unavailable', 'error'].includes(d.meta.status)) { body.replaceChildren(callout('warn', h('strong', 'Query statistics unavailable. '), d.meta.message || '', d.meta.hint ? h('div', d.meta.hint) : ''), metaLine(d.meta)); return; }
      const reg = d.regressions.length ? h('div', { class: 'card mt' }, h('h2', 'Possible regressions and hotspots'), h('p', { class: 'muted small' }, 'Hypotheses from window comparisons. They show a change, not its cause.'),
        d.regressions.map(e => h('div', { class: 'card', style: { padding: '8px 12px', margin: '8px 0', cursor: 'pointer' }, tabindex: 0, role: 'button', onclick: () => { history.replaceState(null, '', `#/evidence/${e.id}`); openEvidence(e.id, ctx); }, onkeydown: ev => { if (ev.key === 'Enter') { ev.preventDefault(); openEvidence(e.id, ctx); } } }, h('div', { class: 'row' }, sev(e.severity), badge(e.confidence, e.confidence), h('strong', e.title)), h('div', { class: 'muted small qtext' }, e.details.text || '')))) : null;
      body.replaceChildren(h('div', { class: 'row small' }, h('span', { class: 'muted' }, `${d.queries_seen} statement(s) with activity · ${d.samples_available} sample(s) stored · stats reset: ${d.stats_reset || 'unknown'}`), h('span', { class: 'spacer' }), metaLine(d.meta)),
        reg, h('div', { class: 'mt' }, dataTable({ caption: 'Top statements', columns: [
          { key: 'text', label: 'Statement', render: r => h('span', { class: 'qtext', title: r.text || '' }, r.text || `(query id ${r.queryid}, text unavailable)`) }, { key: 'calls', label: 'Calls', num: true, render: r => num(r.calls) },
          { key: 'total_ms', label: 'Total time', num: true, render: r => ms(r.total_ms) }, { key: 'mean_ms', label: 'Mean', num: true, tip: 'Window average: delta total time / delta calls', render: r => ms(r.mean_ms) },
          { key: 'share_pct', label: 'Share', num: true, tip: 'Share of sampled statements\' execution time in the window', render: r => r.share_pct != null ? `${r.share_pct}%` : '–' }, { key: 'rows', label: 'Rows/call', num: true, render: r => num(r.rows_per_call, 1) },
          { key: 'cache', label: 'Cache hit', num: true, render: r => r.cache_hit_pct != null ? `${r.cache_hit_pct}%` : '–' }], rows: d.items,
          expand: r => h('div', h('div', { class: 'row' }, copyBtn(r.text || '', 'Copy statement'), r.resets ? badge(`${r.resets} counter reset(s) skipped`, 'warning') : null), h('pre', { class: 'json mt' }, r.text || '(text unavailable)'), h('div', { class: 'mt' }, h('div', { class: 'muted small' }, 'Loading trend…')), kv([['Query id', r.queryid], ['Samples in window', r.samples], ['Max single execution since reset', ms(r.max_exec_ms_since_reset)]])),
          empty: empty('No statement activity in this window', d.samples_available < 2 ? 'At least two samples are needed to compute deltas. The recorder samples every minute by default.' : 'No statements were executed or sampled in this window.') })),
        limitations(d.limitations));
      body.querySelectorAll('tr.clickable').forEach((tr, i) => tr.addEventListener('click', async () => { const sib = tr.nextElementSibling; if (sib && sib.classList.contains('expanded')) { const holder = sib.querySelector('.muted.small'); if (holder && holder.textContent === 'Loading trend…') holder.replaceWith(await trend(d.items[i])); } }));
    } catch (e) { body.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = load; await load(); return root;
}
