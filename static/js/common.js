// Helpers shared by pages.
import { h, icon, sev, badge, dur } from './ui.js';
import { state, range } from './state.js';

export function pageHead(title, sub, actions) {
  return h('div', { class: 'page-head' }, h('div', h('h1', title), sub ? h('p', { class: 'sub' }, sub) : null), actions ? h('div', { class: 'actions' }, actions) : null);
}
export const SEVERITIES = ['critical', 'warning', 'info'];
export function rangeQuery() { const r = range(); return { from: r.from, to: r.to }; }
export const stepFor = r => Math.max(15, Math.round((Date.parse(r.to) - Date.parse(r.from)) / 1000 / 200));
export function refreshButton(onclick, label = 'Refresh now') { return h('button', { class: 'btn', type: 'button', onclick }, icon('refresh', 14), label); }
export function goto(hash) { location.hash = hash; }
export function drill(d) {
  const p = d.page, q = new URLSearchParams();
  for (const [k, v] of Object.entries(d)) if (k !== 'page' && v != null && v !== '') q.set(k, v);
  if (p === 'evidence' && d.id) return `#/evidence/${encodeURIComponent(d.id)}`;
  if (p === 'incidents' && d.id) return `#/incidents/${encodeURIComponent(d.id)}`;
  return `#/${p === 'settings' ? 'settings' : p}${q.toString() ? '?' + q : ''}`;
}
export const sevOrder = { critical: 0, warning: 1, info: 2 };
export function findingRow(e, onOpen) {
  return h('div', { class: 'card', style: { padding: '10px 12px', cursor: 'pointer', marginBottom: '8px' }, tabindex: 0, role: 'button', onclick: () => onOpen(e), onkeydown: ev => { if (ev.key === 'Enter') { ev.preventDefault(); onOpen(e); } } },
    h('div', { class: 'row' }, sev(e.severity), h('strong', e.title)), h('div', { class: 'muted small' }, `${e.category.replace(/_/g, ' ')} · first seen ${dur((Date.now() - Date.parse(e.first_seen)) / 1000)} ago · ${e.observation_count} observation(s)`, e.collection_status === 'stale' ? ' · ' : '', e.collection_status === 'stale' ? badge('stale', 'stale') : null));
}
export function onInstanceOrRange(fn) { return fn; }
export const currentInstance = () => state.instance || undefined;
