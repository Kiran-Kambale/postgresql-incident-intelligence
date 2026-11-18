// Incidents: list + detail with the Detect → Review workflow, evidence, notes, findings with explicit confidence, and reports.
import { get, post, patch, del, inst } from '../api.js';
import { state } from '../state.js';
import { h, icon, badge, sev, timeEl, dur, num, dataTable, pager, debounce, tabs, kv, callout, empty, errorBox, skeleton, toast, confirmDialog, formDialog, copyBtn, copy, jsonView } from '../ui.js';
import { pageHead, goto, drill } from '../common.js';
import { openEvidence } from './evidence.js';

const STAGES = ['detect', 'collect', 'correlate', 'investigate', 'recommend', 'resolve', 'review'];
const STATUS_LABEL = { candidate: 'Candidate', open: 'Open', investigating: 'Investigating', mitigated: 'Mitigated', resolved: 'Resolved', closed: 'Closed', dismissed: 'Dismissed' };
const CLS = ['observed', 'likely', 'hypothesis', 'confirmed'];

export async function render(ctx) { return ctx.arg ? detail(ctx) : list(ctx); }

async function newIncident(prefill = {}) {
  const out = await formDialog({ title: 'New incident', submitText: 'Create incident', fields: [{ name: 'title', label: 'Title', required: true, max: 200, value: prefill.title || '' }, { name: 'symptom', label: 'Symptom (what is the user-visible problem?)', type: 'textarea', max: 4000, value: prefill.symptom || '' },
    { name: 'severity', label: 'Severity', type: 'select', options: ['low', 'medium', 'high', 'critical'], value: prefill.severity || 'medium' }, { name: 'ticket_ref', label: 'Ticket reference', max: 100 }] });
  if (!out) return;
  try { const inc = await post('/api/incidents', { ...out, instance: state.instance || undefined }); toast('Incident created', 'ok'); goto(`#/incidents/${inc.id}`); } catch (e) { toast(e.message, 'err'); }
}

async function list(ctx) {
  const f = { page: 1, size: 20, status: ctx.qs.get('status') || '', q: '' }; const root = h('div'); const box = h('div'); const seg = h('div', { class: 'seg' });
  const TABS = [['', 'All'], ['candidate', 'Candidates'], ['open,investigating,mitigated', 'Active'], ['resolved,closed', 'Resolved'], ['dismissed', 'Dismissed']];
  const drawSeg = counts => seg.replaceChildren(...TABS.map(([v, l]) => h('button', { type: 'button', 'aria-pressed': String(f.status === v), onclick: () => { f.status = v; f.page = 1; load(); } },
    l + (v === 'candidate' && counts && counts.candidate ? ` (${counts.candidate})` : ''))));
  const search = h('input', { type: 'search', 'data-search': '', placeholder: 'Search id, title, ticket…  ( / )', 'aria-label': 'Search incidents' }); search.addEventListener('input', debounce(() => { f.q = search.value; f.page = 1; load(); }, 300));
  root.append(pageHead('Incidents', 'Detect → Collect → Correlate → Investigate → Recommend → Resolve → Review. Candidates are generated from critical evidence and need a human decision.', h('button', { class: 'btn primary', type: 'button', onclick: () => newIncident() }, icon('plus', 14), 'New incident')),
    h('div', { class: 'filters' }, seg, search), box);
  async function load() {
    drawSeg(null); box.replaceChildren(skeleton(3, 50));
    try {
      const d = await get('/api/incidents', { page: f.page, page_size: f.size, status: f.status || undefined, q: f.q || undefined, instance: state.instance || undefined }); drawSeg(d.counts);
      box.replaceChildren(dataTable({ caption: 'Incidents', columns: [{ key: 's', label: 'Status', render: i => badge(STATUS_LABEL[i.status], i.status) }, { key: 'sev', label: 'Severity', render: i => badge(i.severity, i.severity) },
        { key: 't', label: 'Title', render: i => h('div', h('strong', i.title), i.source === 'auto' ? [' ', badge('auto-detected', 'neutral')] : null, i.is_demo ? [' ', badge('DEMO', 'warning')] : null, h('div', { class: 'muted small' }, i.id)) }, { key: 'st', label: 'Stage', render: i => i.stage },
        { key: 'e', label: 'Evidence', num: true, render: i => num(i.evidence_count) }, { key: 'c', label: 'Started', render: i => timeEl(i.started_at || i.created_at) }], rows: d.items, onRow: i => goto(`#/incidents/${i.id}`),
        empty: empty('No incidents', f.status === 'candidate' ? 'No automatic candidates yet. They appear when critical evidence (blocking, saturation, slot risk…) is detected.' : 'Create one manually, or promote a candidate.') }),
        pager({ page: d.page, pages: d.pages, total: d.total, pageSize: d.page_size, onPage: p => { f.page = p; load(); } }));
    } catch (e) { box.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = load; await load(); return root;
}

async function detail(ctx) {
  const id = ctx.arg; const root = h('div'); const body = h('div', skeleton(3, 90));
  root.append(h('div', { class: 'row', style: { marginBottom: '8px' } }, h('a', { href: '#/incidents' }, '← All incidents')), body);
  async function load() {
    try {
      const inc = await get(`/api/incidents/${encodeURIComponent(id)}`); const evs = await get(`/api/incidents/${encodeURIComponent(id)}/evidence`);
      body.replaceChildren(header(inc), stepper(inc), h('div', { class: 'mt' }, tabs([{ id: 'summary', label: 'Summary', render: () => summary(inc) }, { id: 'evidence', label: `Evidence (${evs.total})`, render: () => evidenceTab(inc, evs) }, { id: 'timeline', label: 'Timeline and notes', render: () => timelineTab(inc) },
        { id: 'findings', label: 'Findings and actions', render: () => findingsTab(inc) }, { id: 'report', label: 'Report', render: () => reportTab(inc) }], ctx.qs.get('tab') || 'summary')));
      document.title = `${inc.id} · DB Incident Replay`;
    } catch (e) { body.replaceChildren(errorBox(e, load)); }
  }
  function header(inc) {
    const act = [];
    const setStatus = async (status, extra = {}) => { try { await patch(`/api/incidents/${inc.id}`, { status, ...extra }); toast(`Status: ${STATUS_LABEL[status]}`, 'ok'); load(); } catch (e) { toast(e.message, 'err'); } };
    if (inc.status === 'candidate') { act.push(h('button', { class: 'btn primary', type: 'button', onclick: () => setStatus('open') }, 'Promote to open incident'), h('button', { class: 'btn', type: 'button', onclick: async () => { if (await confirmDialog({ title: 'Dismiss this candidate?', body: 'It will be marked dismissed and will not be recreated for the same condition during the cool-down. You can still reopen it later.', confirmText: 'Dismiss' })) setStatus('dismissed'); } }, 'Dismiss')); }
    if (inc.status === 'open') act.push(h('button', { class: 'btn primary', type: 'button', onclick: () => setStatus('investigating') }, 'Start investigating'));
    if (['open', 'investigating'].includes(inc.status)) act.push(h('button', { class: 'btn', type: 'button', onclick: () => setStatus('mitigated') }, 'Mark mitigated'));
    if (['open', 'investigating', 'mitigated'].includes(inc.status)) act.push(h('button', { class: 'btn', type: 'button', onclick: async () => { const o = await formDialog({ title: 'Resolve incident', submitText: 'Mark resolved', fields: [{ name: 'resolution', label: 'Resolution (what ended the impact?)', type: 'textarea', required: true, value: inc.resolution }] }); if (o) setStatus('resolved', { resolution: o.resolution }); } }, 'Resolve…'));
    if (inc.status === 'resolved') act.push(h('button', { class: 'btn', type: 'button', onclick: () => setStatus('closed') }, 'Close after review'));
    if (['resolved', 'closed', 'dismissed'].includes(inc.status)) act.push(h('button', { class: 'btn', type: 'button', onclick: () => setStatus('investigating') }, 'Reopen'));
    act.push(h('button', { class: 'btn', type: 'button', onclick: async () => { try { const r = await post(`/api/incidents/${inc.id}/capture`); toast(`Snapshot #${r.snapshot_id} captured`, 'ok'); load(); } catch (e) { toast(e.message, 'err'); } } }, 'Capture evidence snapshot'));
    return h('div', { class: 'page-head' }, h('div', h('div', { class: 'row' }, badge(STATUS_LABEL[inc.status], inc.status), badge(inc.severity, inc.severity), inc.source === 'auto' ? badge('auto-detected', 'neutral') : null, inc.is_demo ? badge('DEMO', 'warning') : null), h('h1', inc.title), h('p', { class: 'sub' }, `${inc.id} · instance ${inc.instance_id}${inc.ticket_ref ? ' · ticket ' + inc.ticket_ref : ''}`)), h('div', { class: 'actions' }, act));
  }
  function stepper(inc) {
    const cur = STAGES.indexOf(inc.stage);
    return h('div', { class: 'steps', role: 'group', 'aria-label': 'Workflow stage' }, STAGES.map((s, i) => h('button', { class: `step ${i < cur ? 'done' : ''} ${i === cur ? 'cur' : ''}`, type: 'button', 'aria-current': i === cur ? 'step' : null, title: 'Set workflow stage', onclick: async () => { try { await patch(`/api/incidents/${inc.id}`, { stage: s }); load(); } catch (e) { toast(e.message, 'err'); } } }, `${i + 1}. ${s}`)));
  }
  function editField(inc, key, label, hint) {
    const ta = h('textarea', { rows: 3, maxlength: 4000, 'aria-label': label }, inc[key] || '');
    return h('div', { class: 'field' }, h('label', label), ta, hint ? h('span', { class: 'hint' }, hint) : null, h('div', h('button', { class: 'btn small', type: 'button', onclick: async () => { try { await patch(`/api/incidents/${inc.id}`, { [key]: ta.value }); toast('Saved', 'ok'); load(); } catch (e) { toast(e.message, 'err'); } } }, 'Save')));
  }
  function summary(inc) {
    return h('div', { class: 'grid g2' }, h('div', { class: 'card' }, h('h2', 'Symptom'), h('p', inc.symptom), kv([['Started', timeEl(inc.started_at)], ['Ended', inc.ended_at ? timeEl(inc.ended_at) : 'ongoing'], ['Duration', inc.ended_at ? dur((Date.parse(inc.ended_at) - Date.parse(inc.started_at)) / 1000) : dur((Date.now() - Date.parse(inc.started_at)) / 1000) + ' so far'], ['Environment', inc.environment || '–'], ['Ticket', inc.ticket_ref || '–']])),
      h('div', { class: 'card' }, h('h2', 'Root cause'), callout('', 'A suspected cause is a hypothesis. Mark a cause as confirmed only when you have verified it (for example by reproducing it or confirming with the owner).'),
        editField(inc, 'suspected_root_cause', 'Suspected root cause (hypothesis)'), editField(inc, 'confirmed_root_cause', 'Confirmed root cause'), editField(inc, 'resolution', 'Resolution'), editField(inc, 'review_notes', 'Review notes (what would prevent a repeat?)')));
  }
  function evidenceTab(inc, evs) {
    return h('div', h('div', { class: 'row', style: { marginBottom: '8px' } }, h('span', { class: 'muted' }, 'Evidence linked automatically is labelled; structural links (shared PID/statement) are stronger than temporal proximity.'), h('span', { class: 'spacer' }), h('a', { class: 'btn small', href: '#/evidence' }, 'Find evidence to add')),
      dataTable({ caption: 'Linked evidence', columns: [{ key: 's', label: 'Severity', render: e => sev(e.severity) }, { key: 't', label: 'Title', render: e => e.title }, { key: 'c', label: 'Category', render: e => e.category.replace(/_/g, ' ') }, { key: 'f', label: 'First seen', render: e => timeEl(e.first_seen) },
        { key: 'l', label: 'Link', render: e => badge(e.link_type.replace('_', ' '), e.link_type === 'manual' ? 'confirmed' : 'neutral') }, { key: 'u', label: '', render: e => h('button', { class: 'btn small', type: 'button', onclick: async ev => { ev.stopPropagation(); if (await confirmDialog({ title: 'Unlink evidence?', body: 'The evidence itself is kept; only its link to this incident is removed.', confirmText: 'Unlink' })) { await del(`/api/incidents/${inc.id}/evidence/${e.id}`); load(); } } }, 'Unlink') }],
        rows: evs.items, onRow: e => { openEvidence(e.id, ctx); }, empty: empty('No evidence linked', 'Link evidence from the Evidence Explorer using "Add to incident…".') }));
  }
  function timelineTab(inc) {
    const ta = h('textarea', { rows: 3, maxlength: 4000, placeholder: 'Add a note to the timeline…', 'aria-label': 'New note' });
    const kind = h('select', { 'aria-label': 'Note kind' }, ['note', 'finding', 'action', 'hypothesis'].map(k => h('option', { value: k }, k)));
    return h('div', h('div', { class: 'field' }, ta, h('div', { class: 'row' }, kind, h('button', { class: 'btn primary small', type: 'button', onclick: async () => { if (!ta.value.trim()) return; try { await post(`/api/incidents/${inc.id}/notes`, { text: ta.value, kind: kind.value }); load(); } catch (e) { toast(e.message, 'err'); } } }, 'Add note'))),
      h('ul', { class: 'timeline-list' }, [...inc.timeline_notes].reverse().map(n => h('li', { class: n.kind === 'confirmed' ? 'ok' : n.kind === 'hypothesis' ? 'warning' : '' }, h('div', { class: 'row small' }, timeEl(n.ts), badge(n.kind, n.kind === 'confirmed' ? 'confirmed' : n.kind === 'hypothesis' ? 'hypothesis' : 'neutral'), n.author ? h('span', { class: 'muted' }, n.author) : null), h('div', n.text)))));
  }
  function findingsTab(inc) {
    const add = async () => { const o = await formDialog({ title: 'Record a finding', submitText: 'Record', intro: 'Pick the confidence honestly: confirmed means verified, hypothesis means not yet proven.', fields: [{ name: 'summary', label: 'Finding', type: 'textarea', required: true }, { name: 'classification', label: 'Confidence', type: 'select', options: CLS, value: 'hypothesis' }] }); if (o) { try { await patch(`/api/incidents/${inc.id}`, { finding: o }); load(); } catch (e) { toast(e.message, 'err'); } } };
    const act = async () => { const o = await formDialog({ title: 'Record an action', submitText: 'Record', intro: 'This records what was done or decided. DB Incident Replay never executes actions.', fields: [{ name: 'summary', label: 'Action', type: 'textarea', required: true }] }); if (o) { try { await patch(`/api/incidents/${inc.id}`, { action: o }); load(); } catch (e) { toast(e.message, 'err'); } } };
    return h('div', { class: 'grid g2' }, h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Findings'), h('button', { class: 'btn small', type: 'button', onclick: add }, icon('plus', 13), 'Add')), inc.findings.length ? inc.findings.map(f => h('div', { style: { margin: '8px 0' } }, h('div', { class: 'row' }, badge(f.classification, f.classification), h('span', { class: 'muted small' }, f.recorded_at ? `${new Date(f.recorded_at).toISOString().slice(0, 16)}Z` : '')), h('div', f.summary || f.text || JSON.stringify(f)))) : empty('No findings yet', 'Record what you establish, with its confidence.')),
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', 'Actions taken'), h('button', { class: 'btn small', type: 'button', onclick: act }, icon('plus', 13), 'Add')), inc.actions.length ? inc.actions.map(a => h('div', { style: { margin: '8px 0' } }, h('div', { class: 'row' }, badge('recorded only', 'neutral')), h('div', a.summary || a.text || JSON.stringify(a)))) : empty('No actions recorded', 'Actions are records of what people did; none are executed by this tool.')));
  }
  function reportTab(inc) {
    const box = h('div', skeleton(2, 80));
    get(`/api/incidents/${inc.id}/report`).then(r => {
      const sec = r.sections; const list = (items, fmt) => items.length ? h('ul', items.map(fmt)) : h('p', { class: 'muted' }, 'none recorded');
      box.replaceChildren(h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: async () => { const t = await fetch(`/api/incidents/${inc.id}/report?format=markdown`, { headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} }).then(x => x.text()); copy(t, 'Markdown report copied'); } }, icon('copy', 14), 'Copy as Markdown'),
        h('button', { class: 'btn', type: 'button', onclick: async () => { const t = await fetch(`/api/incidents/${inc.id}/report?format=markdown`, { headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} }).then(x => x.text()); const a = h('a', { href: URL.createObjectURL(new Blob([t], { type: 'text/markdown' })), download: `${inc.id}.md` }); document.body.append(a); a.click(); a.remove(); } }, 'Download .md')),
        callout('', r.note), h('h3', { class: 'mt' }, 'Observed facts'), list(sec.observed_facts, f => h('li', `${f.title} `, badge(f.severity, f.severity), ` (${f.evidence_id})`)), h('h3', 'Likely explanations'), list(sec.likely_explanations, f => h('li', f.title)),
        h('h3', 'Hypotheses (not confirmed)'), list(sec.hypotheses, f => h('li', f.title || f.text, f.limitations ? h('div', { class: 'muted small' }, f.limitations) : null)), h('h3', 'Confirmed findings'), list(sec.confirmed_findings, f => h('li', f.text)),
        h('h3', 'Recommended next steps'), list(r.recommendations, x => h('li', badge(x.class.replace('_', ' '), x.class === 'potentially_disruptive' ? 'critical' : x.class === 'investigation_required' ? 'warning' : 'info'), ' ', x.text, x.risk ? h('div', { class: 'muted small' }, 'Risk: ' + x.risk) : null)));
    }).catch(e => box.replaceChildren(errorBox(e)));
    return box;
  }
  await load(); ctx.tick = null; return root;
}
