// Blocking graph: root blockers on the left, sessions they block to the right. Arrows point from blocker to blocked.
import { h, s, dur } from './ui.js';

const NW = 230, NH = 74, GX = 80, GY = 14, PAD = 12, MAXROWS = 14;
export function blockingGraph(graph, { onSelect, selected } = {}) {
  const rootInfo = new Map(graph.roots.map(r => [r.pid, r]));
  graph = { ...graph, nodes: graph.nodes.map(n => ({ ...n, ...(rootInfo.get(n.pid) || {}) })) };    // roots carry blocked_count / chain
  const level = new Map(); graph.roots.forEach(r => level.set(r.pid, 0));
  for (let i = 0; i < 12; i++) { let changed = false; for (const e of graph.edges) { if (!level.has(e.blocker) && !graph.cycles.flat().includes(e.blocker)) continue; const l = (level.get(e.blocker) ?? 0) + 1; if ((level.get(e.blocked) ?? -1) < l) { level.set(e.blocked, l); changed = true; } } if (!changed) break; }
  graph.nodes.forEach(n => { if (!level.has(n.pid)) level.set(n.pid, 0); });
  const cols = new Map(); for (const n of graph.nodes) { const l = level.get(n.pid); if (!cols.has(l)) cols.set(l, []); cols.get(l).push(n); }
  const pos = new Map(); let maxRows = 1; const hiddenCount = new Map();
  [...cols.entries()].sort((a, b) => a[0] - b[0]).forEach(([l, arr]) => {
    arr.sort((a, b) => (b.blocked_count || 0) - (a.blocked_count || 0) || (b.wait_s || 0) - (a.wait_s || 0));
    arr.slice(0, MAXROWS).forEach((n, i) => pos.set(n.pid, { x: PAD + l * (NW + GX), y: PAD + i * (NH + GY) }));
    if (arr.length > MAXROWS) hiddenCount.set(l, arr.length - MAXROWS); maxRows = Math.max(maxRows, Math.min(arr.length, MAXROWS) + (arr.length > MAXROWS ? 1 : 0));
  });
  const levels = Math.max(...cols.keys(), 0) + 1; const W = PAD * 2 + levels * NW + (levels - 1) * GX, H = PAD * 2 + maxRows * (NH + GY);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, style: 'display:block', role: 'group', 'aria-label': `Blocking graph with ${graph.nodes.length} sessions` },
    s('defs', s('marker', { id: 'arr', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0 0L10 5L0 10z', fill: 'var(--muted)' }))));
  graph.edges.forEach(e => {
    const a = pos.get(e.blocker), b = pos.get(e.blocked); if (!a || !b) return;
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    svg.append(s('path', { d: `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2 - 2} ${y2}`, fill: 'none', stroke: 'var(--muted)', 'stroke-width': 1.5, 'marker-end': 'url(#arr)' }, s('title', `PID ${e.blocker} blocks PID ${e.blocked}`)));
  });
  graph.nodes.forEach(n => {
    const p = pos.get(n.pid); if (!p) return;
    const root = n.role === 'root_blocker', mid = n.role === 'intermediate';
    const stroke = root ? 'var(--crit)' : mid ? 'var(--warn)' : 'var(--line)';
    const g = s('g', { class: 'node', transform: `translate(${p.x},${p.y})`, tabindex: 0, role: 'button', 'aria-label': `PID ${n.pid}, ${n.role.replace('_', ' ')}`, onclick: () => onSelect && onSelect(n), onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect && onSelect(n); } } },
      s('rect', { width: NW, height: NH, rx: 9, fill: 'var(--panel)', stroke: selected === n.pid ? 'var(--accent)' : stroke, 'stroke-width': selected === n.pid || root ? 2.5 : 1.5 }),
      s('text', { x: 10, y: 20, 'font-size': 13, 'font-weight': 700, fill: 'var(--text)' }, `PID ${n.pid} · ${root ? 'ROOT BLOCKER' : mid ? 'blocked + blocking' : 'blocked'}`),
      s('text', { x: 10, y: 39, 'font-size': 12, fill: 'var(--muted)' }, `${(n.application_name || 'no app name').slice(0, 20)} / ${(n.usename || '?').slice(0, 14)}`),
      s('text', { x: 10, y: 57, 'font-size': 12, fill: root ? 'var(--crit)' : 'var(--muted)' }, root ? `${n.state || 'unknown'} · xact ${dur(n.xact_age_s)}` : `waiting ≥ ${dur(n.wait_s)}`),
      s('text', { x: 10, y: 70, 'font-size': 10.5, fill: 'var(--muted)' }, root ? `blocks ${n.blocked_count ?? '?'} session${n.blocked_count === 1 ? '' : 's'} in total` : n.direct_blockers.length ? `directly blocked by ${n.direct_blockers.join(', ')}` : ''));
    svg.append(g);
  });
  hiddenCount.forEach((c, l) => svg.append(s('text', { x: PAD + l * (NW + GX), y: PAD + MAXROWS * (NH + GY) + 14, 'font-size': 12, fill: 'var(--muted)' }, `+${c} more session(s) not drawn`)));
  const legend = h('div', { class: 'legend' }, h('span', { style: { color: 'var(--crit)' } }, '■ root blocker (not waiting)'), h('span', { style: { color: 'var(--warn)' } }, '■ blocked and blocking (intermediate)'), h('span', '■ blocked (leaf)'), h('span', 'arrow: blocker → blocked session'));
  return h('div', legend, h('div', { style: { overflow: 'auto', maxHeight: '600px', border: '1px solid var(--line)', borderRadius: '8px', background: 'var(--bg)' } }, svg));
}
