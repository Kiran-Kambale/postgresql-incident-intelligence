// Dependency-free SVG charts. Gaps in collection are drawn explicitly and lines are never interpolated across them.
import { h, s, fmtTime, num, tzLabel } from './ui.js';

const COLORS = ['#4c8dff', '#f0b43c', '#3fb68b', '#ff6b6b', '#b48cff', '#4dd0e1'];
export const color = i => COLORS[i % COLORS.length];

function niceTicks(max, n = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / n; const mag = 10 ** Math.floor(Math.log10(raw)); const f = raw / mag;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag; const out = [];
  for (let v = 0; v <= max + step * 0.999; v += step) out.push(+v.toFixed(10)); return out;
}

export function lineChart({ series, gaps = [], from, to, height = 190, fmt = v => num(v, 1), thresholds = [], onBrush, step = 60, area = false, empty = 'No samples recorded in this range.' }) {
  const wrap = h('div', { class: 'chart' }); const hidden = new Set();
  const t0 = Date.parse(from), t1 = Date.parse(to);
  const legend = h('div', { class: 'legend' }, series.map((sr, i) => h('button', { type: 'button', 'aria-pressed': 'true', onclick: e => { hidden.has(sr.name) ? hidden.delete(sr.name) : hidden.add(sr.name); e.currentTarget.setAttribute('aria-pressed', String(!hidden.has(sr.name))); draw(); } }, h('i', { style: { background: sr.color || color(i) } }), sr.name)));
  const host = h('div'); const tipEl = h('div', { class: 'tip', hidden: true }); wrap.append(legend, host, tipEl);
  function draw() {
    const W = Math.max(280, host.clientWidth || wrap.clientWidth || 640), H = height, ml = 46, mr = 10, mt = 8, mb = 22;
    const pw = W - ml - mr, ph = H - mt - mb;
    const vis = series.filter(x => !hidden.has(x.name));
    const pts = vis.flatMap(x => x.points.filter(p => p.value != null && !Number.isNaN(p.value)));
    if (!series.some(x => x.points.some(p => p.value != null))) { host.replaceChildren(h('div', { class: 'empty', style: { padding: '20px' } }, empty, gaps.length ? h('div', { class: 'small mt' }, `Collection gaps in range: ${gaps.length}`) : null)); return; }
    const yMax = Math.max(1e-9, ...pts.map(p => p.value), ...thresholds.map(t => t.value)) * 1.1;
    const ticks = niceTicks(yMax, 4); const top = ticks[ticks.length - 1];
    const X = t => ml + ((t - t0) / (t1 - t0)) * pw, Y = v => mt + ph - (v / top) * ph;
    const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': 'Time series chart' });
    ticks.forEach(v => { svg.append(s('line', { x1: ml, x2: W - mr, y1: Y(v), y2: Y(v), stroke: 'var(--grid)' }), s('text', { x: ml - 6, y: Y(v) + 4, 'text-anchor': 'end', fill: 'var(--muted)', 'font-size': 11 }, fmt(v))); });
    const n = 5; for (let i = 0; i <= n; i++) { const t = t0 + (t1 - t0) * i / n; svg.append(s('text', { x: X(t), y: H - 6, 'text-anchor': i === 0 ? 'start' : i === n ? 'end' : 'middle', fill: 'var(--muted)', 'font-size': 11 }, fmtTime(new Date(t).toISOString(), (t1 - t0) > 36 * 3600e3))); }
    svg.append(s('defs', s('pattern', { id: 'hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, s('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'var(--muted)', 'stroke-width': 2, opacity: 0.5 }))));
    gaps.forEach(g => { const a = Math.max(t0, Date.parse(g.from)), b = Math.min(t1, Date.parse(g.to)); if (b <= a) return; svg.append(s('rect', { x: X(a), y: mt, width: Math.max(2, X(b) - X(a)), height: ph, fill: 'url(#hatch)', opacity: g.kind === 'before_monitoring' ? 0.5 : 0.9 }, s('title', `${g.kind === 'before_monitoring' ? 'Before monitoring started' : 'No data collected'}: ${fmtTime(g.from)} to ${fmtTime(g.to)}`))); });
    thresholds.forEach(t => svg.append(s('line', { x1: ml, x2: W - mr, y1: Y(t.value), y2: Y(t.value), stroke: 'var(--warn)', 'stroke-dasharray': '5 4' }), s('text', { x: W - mr - 2, y: Y(t.value) - 3, 'text-anchor': 'end', fill: 'var(--warn)', 'font-size': 11 }, t.label)));
    vis.forEach((sr, i) => {
      const col = sr.color || color(series.indexOf(sr)); let d = ''; let prev = null; const ar = [];
      sr.points.forEach(p => {
        if (p.value == null || Number.isNaN(p.value)) { prev = null; return; }
        const t = Date.parse(p.ts); const x = X(t), y = Y(p.value);
        d += (prev === null || (t - prev) > step * 2500) ? `M${x.toFixed(1)} ${y.toFixed(1)}` : `L${x.toFixed(1)} ${y.toFixed(1)}`; prev = t; ar.push([x, y]);
      });
      if (area && i === 0 && ar.length > 1) svg.append(s('path', { d: `${d}L${ar[ar.length - 1][0]} ${Y(0)}L${ar[0][0]} ${Y(0)}Z`, fill: col, opacity: 0.12 }));
      svg.append(s('path', { d, fill: 'none', stroke: col, 'stroke-width': 1.8, 'stroke-linejoin': 'round' }));
      if (ar.length === 1) svg.append(s('circle', { cx: ar[0][0], cy: ar[0][1], r: 3, fill: col }));
    });
    const cursor = s('line', { y1: mt, y2: mt + ph, stroke: 'var(--muted)', 'stroke-dasharray': '3 3', visibility: 'hidden' }); const sel = s('rect', { y: mt, height: ph, fill: 'var(--accent)', opacity: 0.18, visibility: 'hidden' });
    const overlay = s('rect', { x: ml, y: mt, width: pw, height: ph, fill: 'transparent', style: 'cursor:crosshair' });
    svg.append(cursor, sel, overlay);
    const all = [...new Set(vis.flatMap(x => x.points.map(p => Date.parse(p.ts))))].sort((a, b) => a - b);
    let dragStart = null;
    const px = ev => { const r = svg.getBoundingClientRect(); return (ev.clientX - r.left) * (W / r.width); };
    overlay.addEventListener('mousemove', ev => {
      const x = px(ev); const t = t0 + ((x - ml) / pw) * (t1 - t0);
      if (dragStart !== null) { sel.setAttribute('x', Math.min(dragStart, x)); sel.setAttribute('width', Math.abs(x - dragStart)); sel.setAttribute('visibility', 'visible'); }
      let best = null; for (const c of all) if (best === null || Math.abs(c - t) < Math.abs(best - t)) best = c;
      if (best === null || Math.abs(best - t) > Math.max(step * 3000, (t1 - t0) / 60)) { cursor.setAttribute('visibility', 'hidden'); tipEl.hidden = true; return; }
      cursor.setAttribute('x1', X(best)); cursor.setAttribute('x2', X(best)); cursor.setAttribute('visibility', 'visible');
      const rows = vis.map(sr => { const p = sr.points.find(q => Date.parse(q.ts) === best); return p && p.value != null ? `${sr.name}: ${fmt(p.value)}` : null; }).filter(Boolean);
      tipEl.textContent = ''; tipEl.append(h('div', fmtTime(new Date(best).toISOString()) + ' ' + tzLabel()), ...rows.map(r => h('div', r)));
      tipEl.hidden = false; const left = Math.min(Math.max(0, X(best) * (host.clientWidth / W) + 10), Math.max(0, host.clientWidth - 190)); tipEl.style.left = `${left}px`; tipEl.style.top = `${legend.offsetHeight + 6}px`;
    });
    overlay.addEventListener('mouseleave', () => { cursor.setAttribute('visibility', 'hidden'); tipEl.hidden = true; if (dragStart !== null) { dragStart = null; sel.setAttribute('visibility', 'hidden'); } });
    if (onBrush) {
      overlay.addEventListener('mousedown', ev => { dragStart = px(ev); });
      overlay.addEventListener('mouseup', ev => { if (dragStart === null) return; const a = Math.min(dragStart, px(ev)), b = Math.max(dragStart, px(ev)); dragStart = null; sel.setAttribute('visibility', 'hidden');
        if (b - a > 8) onBrush(new Date(t0 + ((a - ml) / pw) * (t1 - t0)).toISOString(), new Date(t0 + ((b - ml) / pw) * (t1 - t0)).toISOString()); });
      overlay.addEventListener('dblclick', () => onBrush(null, null));
    }
    host.replaceChildren(svg);
  }
  requestAnimationFrame(draw);
  if (window.ResizeObserver) { let last = 0; new ResizeObserver(() => { const w = host.clientWidth; if (Math.abs(w - last) > 4) { last = w; draw(); } }).observe(host); }
  return wrap;
}

export function sparkline(values, { w = 120, hgt = 32, col = 'var(--accent)' } = {}) {
  const v = values.filter(x => x != null); if (v.length < 2) return h('span', { class: 'muted small' }, '–');
  const max = Math.max(...v), min = Math.min(...v), rng = max - min || 1;
  const pts = v.map((x, i) => `${(i / (v.length - 1) * w).toFixed(1)},${(hgt - 2 - ((x - min) / rng) * (hgt - 4)).toFixed(1)}`).join(' ');
  return s('svg', { viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, 'aria-hidden': 'true' }, s('polyline', { points: pts, fill: 'none', stroke: col, 'stroke-width': 1.6 }));
}
export function bars(items, { max, fmt = v => num(v), col = 'var(--accent)' } = {}) {
  const m = max ?? Math.max(1, ...items.map(i => i.value));
  return h('div', items.map(i => h('div', { style: { marginBottom: '8px' } }, h('div', { class: 'row small' }, h('span', i.label), h('span', { class: 'spacer' }), h('span', { class: 'muted' }, i.text ?? fmt(i.value))), h('div', { class: `bar ${i.tone || ''}` }, h('i', { style: { width: `${Math.min(100, 100 * i.value / m)}%`, background: i.color || col } })))));
}
