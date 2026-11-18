// Swim-lane event timeline: bars for episodes, diamonds for point events, hatched bands for collection gaps.
import { h, s, fmtTime, dur } from './ui.js';

const SEV = { critical: 'var(--crit)', warning: 'var(--warn)', info: 'var(--info)' };
export function eventTimeline({ events, gaps = [], from, to, onSelect, onBrush, laneH = 24 }) {
  const t0 = Date.parse(from), t1 = Date.parse(to);
  const lanes = new Map(); events.forEach(e => { if (!lanes.has(e.category)) lanes.set(e.category, []); lanes.get(e.category).push(e); });
  const order = [...lanes.entries()].sort((a, b) => (a[0] === 'incident' ? -1 : b[0] === 'incident' ? 1 : b[1].length - a[1].length)).slice(0, 14);
  if (!order.length) return h('div', { class: 'empty' }, h('h3', 'No events in this range'), h('div', 'Nothing was recorded, or collection was not running. Gaps are shown below if known.'),
    gaps.length ? h('div', { class: 'small mt' }, gaps.map(g => h('div', `${g.kind === 'before_monitoring' ? 'Before monitoring started' : 'No data'}: ${fmtTime(g.from)} → ${fmtTime(g.to)} (${dur(g.seconds)})`))) : null);
  const wrap = h('div', { style: { overflow: 'auto' } });
  const W = Math.max(720, wrap.clientWidth || 900), ml = 150, mr = 12, mt = 6, mb = 22, H = mt + order.length * laneH + mb, pw = W - ml - mr;
  const X = t => ml + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * pw;
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: 'min-width:720px', role: 'group', 'aria-label': 'Event timeline' });
  svg.append(s('defs', s('pattern', { id: 'tlhatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, s('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'var(--muted)', 'stroke-width': 2, opacity: 0.55 }))));
  order.forEach(([cat, evs], i) => {
    const y = mt + i * laneH;
    svg.append(s('rect', { x: ml, y, width: pw, height: laneH - 2, fill: i % 2 ? 'transparent' : 'var(--panel2)', opacity: 0.6 }), s('text', { x: ml - 8, y: y + laneH / 2 + 3, 'text-anchor': 'end', 'font-size': 11.5, fill: 'var(--muted)' }, cat.replace(/_/g, ' ') + ` (${evs.length})`));
  });
  gaps.forEach(g => { const a = Date.parse(g.from), b = Date.parse(g.to); if (b < t0 || a > t1) return; svg.append(s('rect', { x: X(a), y: mt, width: Math.max(2, X(b) - X(a)), height: order.length * laneH, fill: 'url(#tlhatch)' }, s('title', `${g.kind === 'before_monitoring' ? 'Before monitoring started' : 'No data collected'}: ${fmtTime(g.from)} → ${fmtTime(g.to)}`))); });
  order.forEach(([cat, evs], i) => evs.forEach(e => {
    const y = mt + i * laneH + 3, st = Date.parse(e.start), en = e.end ? Date.parse(e.end) : Math.min(Date.now(), t1);
    if (en < t0 || st > t1) return;
    const fill = SEV[e.severity] || 'var(--muted)'; const label = `${e.title}\n${fmtTime(e.start)}${e.instant ? '' : ' → ' + (e.end ? fmtTime(e.end) : 'ongoing')}${e.stale ? '\n(stale: its collector is failing)' : ''}`;
    const open = () => onSelect && onSelect(e);
    const common = { tabindex: 0, role: 'button', 'aria-label': e.title, style: 'cursor:pointer', onclick: open, onkeydown: ev => { if (ev.key === 'Enter') { ev.preventDefault(); open(); } } };
    if (e.instant) { const x = X(st); svg.append(s('path', { d: `M${x} ${y} l6 7 -6 7 -6 -7z`, fill, ...common }, s('title', label))); }
    else svg.append(s('rect', { x: X(st), y, width: Math.max(4, X(en) - X(st)), height: laneH - 8, rx: 3, fill, opacity: e.stale ? 0.5 : 0.9, 'stroke-dasharray': e.ongoing ? '4 2' : null, stroke: e.ongoing ? 'var(--text)' : null, ...common }, s('title', label)));
  }));
  for (let k = 0; k <= 6; k++) { const t = t0 + (t1 - t0) * k / 6; svg.append(s('text', { x: X(t), y: H - 6, 'text-anchor': k === 0 ? 'start' : k === 6 ? 'end' : 'middle', 'font-size': 11, fill: 'var(--muted)' }, fmtTime(new Date(t).toISOString(), (t1 - t0) > 36 * 3600e3))); }
  if (onBrush) {
    const sel = s('rect', { y: mt, height: order.length * laneH, fill: 'var(--accent)', opacity: 0.18, visibility: 'hidden' }); let ds = null;
    const px = ev => { const r = svg.getBoundingClientRect(); return (ev.clientX - r.left) * (W / r.width); };
    const layer = s('rect', { x: ml, y: mt, width: pw, height: order.length * laneH, fill: 'transparent', style: 'cursor:crosshair' });
    svg.insertBefore(layer, svg.children[1]);
    svg.append(sel);
    layer.addEventListener('mousedown', ev => { ds = px(ev); }); layer.addEventListener('mousemove', ev => { if (ds === null) return; const x = px(ev); sel.setAttribute('x', Math.min(ds, x)); sel.setAttribute('width', Math.abs(x - ds)); sel.setAttribute('visibility', 'visible'); });
    layer.addEventListener('mouseup', ev => { if (ds === null) return; const a = Math.min(ds, px(ev)), b = Math.max(ds, px(ev)); ds = null; sel.setAttribute('visibility', 'hidden'); if (b - a > 8) onBrush(new Date(t0 + ((a - ml) / pw) * (t1 - t0)).toISOString(), new Date(t0 + ((b - ml) / pw) * (t1 - t0)).toISOString()); });
    layer.addEventListener('dblclick', () => onBrush(null, null));
  }
  wrap.append(svg);
  return h('div', h('div', { class: 'legend' }, h('span', { style: { color: 'var(--crit)' } }, '■ critical'), h('span', { style: { color: 'var(--warn)' } }, '■ warning'), h('span', { style: { color: 'var(--info)' } }, '■ info'), h('span', '◆ point event'), h('span', 'dashed outline: still active'), h('span', '▨ no data collected'), h('span', 'drag to zoom · double-click to reset')), wrap);
}
