// Evidence Explorer + the shared evidence drawer (diagnostic card). Evidence is shown exactly as recorded; nothing is inferred client-side.
import { get, post, inst, api } from '../api.js';
import { state, range, set } from '../state.js';
import { h, icon, sev, badge, timeEl, dur, dataTable, pager, chips, debounce, tabs, openDrawer, closeDrawer, jsonView, kv, copy, copyBtn, confirmDialog, toast, callout, empty, errorBox, skeleton, num, formDialog } from '../ui.js';
import { pageHead, SEVERITIES, drill, goto } from '../common.js';
import { eventTimeline } from '../timeline.js';

const CLASS_LABEL = { informational: 'Informational', investigation_required: 'Investigation required', potentially_disruptive: 'Potentially disruptive' };
const CLASS_KIND = { informational: 'info', investigation_required: 'warning', potentially_disruptive: 'critical' };
const EPI = { observed_fact: 'Observed fact', likely_explanation: 'Likely explanation', hypothesis: 'Hypothesis (not confirmed)', confirmed_finding: 'Confirmed finding' };

export async function openEvidence(id, ctx) {
  const body = h('div', skeleton(3, 80));
  openDrawer(`Evidence ${id}`, body, { onClose: () => { if (location.hash.startsWith('#/evidence/')) history.replaceState(null, '', '#/evidence'); } });
  try {
    const d = await get(`/api/evidence/${encodeURIComponent(id)}`);
    body.replaceChildren(diagnosticCard(d, ctx));
  } catch (e) { body.replaceChildren(errorBox(e, () => openEvidence(id, ctx))); }
}

function diagnosticCard(d, ctx) {
  const ev = d.evidence, c = d.card;
  const head = h('div', { class: 'row' }, sev(ev.severity), badge(ev.category.replace(/_/g, ' '), 'neutral'), badge(ev.status, ev.status), badge(EPI[c.epistemic] || c.epistemic, c.epistemic),
    ev.is_demo ? badge('DEMO', 'warning') : null, ev.collection_status === 'stale' ? badge('stale: collector failing', 'stale') : null);
  const actions = h('div', { class: 'row mt' }, copyBtn(ev.id, 'Copy ID'),
    h('button', { class: 'btn small', type: 'button', onclick: () => copy(`${location.origin}${location.pathname}#/evidence/${ev.id}`, 'Link copied') }, icon('link', 14), 'Copy link'),
    h('button', { class: 'btn small', type: 'button', onclick: () => addToIncident(ev, ctx) }, icon('plus', 14), 'Add to incident…'),
    h('button', { class: 'btn small', type: 'button', onclick: () => { closeDrawer(); goto(`#/evidence?from=${encodeURIComponent(new Date(Date.parse(ev.first_seen) - 600000).toISOString())}&to=${encodeURIComponent(new Date(Date.parse(ev.cleared_at || ev.last_seen) + 600000).toISOString())}`); } }, 'Surrounding events'));
  const diagnosis = () => h('div',
    h('h3', '1. What was observed?'), h('ul', c.observed.map(o => h('li', o))),
    h('h3', { class: 'mt' }, '2. Why does it matter?'), h('p', c.why_it_matters),
    h('h3', '3. What evidence supports it?'),
    kv([['Source', c.evidence.source], ['First observed', timeEl(c.evidence.first_seen)], ['Last observed', timeEl(c.evidence.last_seen)], ev.cleared_at ? ['Cleared', timeEl(ev.cleared_at)] : ['Status', 'Still active at the last collection'],
      ['Observations', num(c.evidence.observations)], ['Collection', badge(c.evidence.collection_status === 'ok' ? 'ok (last observation succeeded)' : 'stale (collector failing)', c.evidence.collection_status === 'ok' ? 'ok' : 'stale')], ['Fingerprint', h('span', { class: 'mono' }, ev.fingerprint)]]),
    c.limitations ? callout('warn', h('strong', 'Limitations: '), c.limitations) : null,
    h('h3', { class: 'mt' }, '4. What is the likely impact?'), h('p', c.impact),
    h('h3', '5. What should the engineer investigate next?'),
    h('div', { class: 'row' }, c.investigate.map(i => h('a', { class: 'btn small', href: drill(i.drilldown), onclick: () => closeDrawer() }, icon('ext', 13), i.text))),
    h('h3', { class: 'mt' }, '6. Suggested actions and their risks'),
    c.recommendations.map(r => recommendation(r)),
    h('p', { class: 'muted small mt' }, 'Nothing on this page executes SQL. Suggested statements are for you to review and run deliberately.'));
  const tabDefs = [
    { id: 'diag', label: 'Diagnosis', render: diagnosis },
    { id: 'json', label: 'Evidence JSON', render: () => jsonView(ev) },
    { id: 'related', label: 'Related', render: () => relatedPanel(ev, ctx) },
    { id: 'time', label: 'Timestamps', render: () => h('div', kv([['First seen (UTC)', ev.first_seen], ['Last seen (UTC)', ev.last_seen], ['Cleared (UTC)', ev.cleared_at || '–'], ['Duration', dur(ev.duration_s)], ['Created (UTC)', ev.created_at], ['Display time zone', state.tz]]),
      h('p', { class: 'muted small' }, 'All stored times are UTC. The dashboard converts them to the selected time zone; the tooltip on any time shows both.')) },
  ];
  return h('div', head, h('h2', { style: { margin: '10px 0 0' } }, ev.title), actions, h('div', { class: 'mt' }, tabs(tabDefs, 'diag')));
}

function recommendation(r) {
  const box = h('div', { class: 'card', style: { marginBottom: '8px', padding: '10px 12px' } },
    h('div', { class: 'row' }, badge(CLASS_LABEL[r.class], CLASS_KIND[r.class])), h('div', { style: { margin: '6px 0 2px', fontWeight: 600 } }, r.text), h('div', { class: 'muted small' }, 'Why: ' + r.rationale),
    r.risk ? h('div', { class: 'callout crit mt', style: { marginTop: '8px' } }, h('strong', 'Risk: '), r.risk) : null);
  if (r.sql) {
    const pre = h('pre', { class: 'json', style: { marginTop: '8px', maxHeight: '140px' } }, r.sql);
    box.append(pre, h('div', { class: 'row mt', style: { marginTop: '8px' } }, h('button', { class: 'btn small' + (r.class === 'potentially_disruptive' ? ' danger' : ''), type: 'button', onclick: async () => {
      if (r.class === 'potentially_disruptive') { const ok = await confirmDialog({ title: 'Copy a potentially disruptive statement?', danger: true, confirmText: 'Copy SQL', body: h('div', h('p', 'This only copies the text. DB Incident Replay does not run it.'), h('p', h('strong', 'Risk: '), r.risk || 'It may interrupt sessions or roll back work.'), h('p', { class: 'muted small' }, 'Confirm the session\'s owner and impact before running it yourself.')) }); if (!ok) return; }
      copy(r.sql, 'SQL copied. Review before running.'); } }, icon('copy', 14), r.class === 'potentially_disruptive' ? 'Copy SQL…' : 'Copy SQL')));
  }
  return box;
}

function relatedPanel(ev, ctx) {
  const box = h('div', skeleton(2, 60));
  get(`/api/evidence/${encodeURIComponent(ev.id)}/related`, { window_min: 10 }).then(r => {
    box.replaceChildren(callout('', r.note), r.items.length ? h('div', { class: 'mt' }, r.items.map(x => h('div', { class: 'card', style: { padding: '8px 12px', marginBottom: '8px', cursor: 'pointer' }, tabindex: 0, role: 'button', onclick: () => openEvidence(x.id, ctx), onkeydown: e => { if (e.key === 'Enter') { e.preventDefault(); openEvidence(x.id, ctx); } } },
      h('div', { class: 'row' }, sev(x.severity), badge(x.strength, x.strength === 'structural' ? 'confirmed' : 'neutral'), h('strong', x.title)), h('div', { class: 'muted small' }, x.links.map(l => l.why).join(' ')))))
      : empty('Nothing related nearby', 'No other evidence was recorded in the surrounding window.'));
  }).catch(e => box.replaceChildren(errorBox(e)));
  return box;
}

export async function addToIncident(ev, ctx) {
  let list;
  try { list = await get('/api/incidents', { status: 'candidate,open,investigating,mitigated', page_size: 50, instance: ev.instance_id }); } catch (e) { toast(e.message, 'err'); return; }
  const opts = [{ value: '__new', label: '+ New incident from this evidence' }, ...list.items.map(i => ({ value: i.id, label: `${i.id} · ${i.title}`.slice(0, 90) }))];
  const out = await formDialog({ title: 'Add evidence to an incident', submitText: 'Add', fields: [{ name: 'incident', label: 'Incident', type: 'select', options: opts, value: '__new' }] });
  if (!out) return;
  try {
    if (out.incident === '__new') { const inc = await post('/api/incidents', { title: ev.title.slice(0, 190), symptom: `Created from evidence ${ev.id}.`, severity: ev.severity === 'critical' ? 'high' : 'medium', instance: ev.instance_id, evidence_ids: [ev.id] }); toast('Incident created', 'ok'); closeDrawer(); goto(`#/incidents/${inc.id}`); }
    else { await post(`/api/incidents/${out.incident}/evidence`, { evidence_id: ev.id }); toast('Evidence linked', 'ok'); }
  } catch (e) { toast(e.message, 'err'); }
}

// ---------------------------------------------------------------------------------------------------- page
export async function render(ctx) {
  const q = ctx.qs; const f = { page: Number(q.get('page') || 1), size: 25, sort: q.get('sort') || 'first_seen', order: q.get('order') || 'desc', q: q.get('q') || '', cats: new Set((q.get('category') || '').split(',').filter(Boolean)),
    sevs: new Set((q.get('severity') || '').split(',').filter(Boolean)), status: q.get('status') || '', from: q.get('from') || '', to: q.get('to') || '' };
  const root = h('div'); const tableBox = h('div'); const tlBox = h('div'); const filterBox = h('div');
  const search = h('input', { type: 'search', placeholder: 'Search id, title, PID, details…  ( / )', 'data-search': '', 'aria-label': 'Search evidence', value: f.q, style: { minWidth: '280px' } });
  const statusSel = h('select', { 'aria-label': 'Status filter', onchange: () => { f.status = statusSel.value; f.page = 1; load(); } }, [['', 'Status: any'], ['active', 'Active'], ['cleared', 'Cleared']].map(([v, l]) => h('option', { value: v, selected: f.status === v }, l)));
  search.addEventListener('input', debounce(() => { f.q = search.value; f.page = 1; load(); }, 300));
  let facets = { categories: [] };
  const params = () => ({ instance: state.instance || undefined, category: [...f.cats].join(','), severity: [...f.sevs].join(','), status: f.status, q: f.q, sort: f.sort, order: f.order, page: f.page, page_size: f.size, from: f.from || undefined, to: f.to || undefined });
  const drawFilters = () => filterBox.replaceChildren(h('div', { class: 'filters' }, search, statusSel, h('button', { class: 'btn small', type: 'button', onclick: () => { f.cats.clear(); f.sevs.clear(); f.status = ''; f.q = ''; f.from = f.to = ''; search.value = ''; statusSel.value = ''; f.page = 1; load(); } }, 'Clear filters'),
      f.from ? badge(`range ${new Date(f.from).toISOString().slice(11, 16)}–${new Date(f.to).toISOString().slice(11, 16)} UTC`, 'info') : null),
    h('div', { class: 'row small', style: { marginBottom: '6px' } }, h('span', { class: 'muted' }, 'Severity'), chips(SEVERITIES, f.sevs, v => { f.sevs.has(v) ? f.sevs.delete(v) : f.sevs.add(v); f.page = 1; load(); })),
    h('div', { class: 'row small', style: { marginBottom: '10px' } }, h('span', { class: 'muted' }, 'Category'), chips(facets.categories.map(c => ({ value: c, label: c.replace(/_/g, ' ') })), f.cats, v => { f.cats.has(v) ? f.cats.delete(v) : f.cats.add(v); f.page = 1; load(); })));
  async function load() {
    drawFilters(); tableBox.replaceChildren(skeleton(4, 44));
    try {
      const d = await get('/api/evidence', params()); facets = d.facets; drawFilters();
      const cols = [
        { key: 'severity', label: 'Severity', sortable: true, render: e => sev(e.severity) }, { key: 'category', label: 'Category', sortable: true, render: e => e.category.replace(/_/g, ' ') },
        { key: 'title', label: 'Title', sortable: true, render: e => h('span', e.title, e.is_demo ? [' ', badge('DEMO', 'warning')] : null, e.collection_status === 'stale' ? [' ', badge('stale', 'stale')] : null) },
        { key: 'first_seen', label: 'First seen', sortable: true, render: e => timeEl(e.first_seen) }, { key: 'duration', label: 'Duration', render: e => e.status === 'active' ? h('span', badge('active', 'active'), ' ', dur((Date.now() - Date.parse(e.first_seen)) / 1000)) : e.first_seen === e.cleared_at ? h('span', { class: 'muted' }, 'instant') : dur(e.duration_s) },
        { key: 'observations', label: 'Obs.', sortable: true, num: true, render: e => num(e.observation_count) }];
      tableBox.replaceChildren(dataTable({ columns: cols, rows: d.items, sort: { key: f.sort, dir: f.order }, onSort: k => { f.order = f.sort === k && f.order === 'desc' ? 'asc' : 'desc'; f.sort = k; load(); }, onRow: e => { history.replaceState(null, '', `#/evidence/${e.id}`); openEvidence(e.id, ctx); },
        empty: empty('No evidence matches', f.q || f.cats.size || f.sevs.size ? 'Try clearing filters.' : 'Nothing has been recorded yet. Evidence appears as the flight recorder observes noteworthy conditions.'), caption: 'Evidence list' }),
        pager({ page: d.page, pages: d.pages, total: d.total, pageSize: d.page_size, onPage: p => { f.page = p; load(); } }));
    } catch (e) { tableBox.replaceChildren(errorBox(e, load)); }
  }
  async function loadTimeline() {
    const r = f.from ? { from: f.from, to: f.to } : range();
    try {
      const t = await get('/api/timeline', { instance: state.instance || undefined, from: r.from, to: r.to, category: [...f.cats].join(','), severity: [...f.sevs].join(',') });
      tlBox.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Timeline'), h('span', { class: 'muted' }, `coverage ${t.coverage_pct}% · ${t.events.length} event(s)`)),
        eventTimeline({ events: t.events, gaps: t.gaps, from: t.from, to: t.to, onSelect: e => e.type === 'evidence' ? (history.replaceState(null, '', `#/evidence/${e.id}`), openEvidence(e.id, ctx)) : goto(`#/incidents/${e.id}`), onBrush: (a, b) => { f.from = a || ''; f.to = b || ''; f.page = 1; load(); loadTimeline(); } })));
    } catch (e) { tlBox.replaceChildren(errorBox(e)); }
  }
  root.append(pageHead('Evidence Explorer', 'Everything the flight recorder observed. Select a row for the diagnostic card; the timeline shows when things happened and where data is missing.'), tlBox, h('div', { class: 'mt' }, filterBox, tableBox));
  ctx.tick = () => { loadTimeline(); load(); };
  load(); loadTimeline();
  if (ctx.arg) openEvidence(ctx.arg, ctx);
  return root;
}
