// Global UI state with simple subscriptions. Time zone/theme persist locally; the API token lives in sessionStorage only.
const listeners = new Set();
const ls = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
const lset = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } };
export const state = {
  instance: ls('dir.instance', ''),
  rangePreset: ls('dir.range', '1h'),
  customRange: null,                       // {from, to} ISO when the user brushes a chart
  tz: ls('dir.tz', 'UTC'),
  theme: ls('dir.theme', matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'),
  refreshSec: Number(ls('dir.refresh', '10')),
  token: (() => { try { return sessionStorage.getItem('dir.token') || ''; } catch { return ''; } })(),
  server: { demo: false, authRequired: false },
};
export const PRESETS = { '15m': 15, '1h': 60, '6h': 360, '24h': 1440, '7d': 10080 };
export function range() {
  if (state.rangePreset === 'custom' && state.customRange) return { ...state.customRange, label: 'custom' };
  const to = new Date(); const from = new Date(to.getTime() - (PRESETS[state.rangePreset] || 60) * 60000);
  return { from: from.toISOString(), to: to.toISOString(), label: state.rangePreset };
}
export function set(patch, { persist = true, notify = true } = {}) {
  Object.assign(state, patch);
  if (persist) {
    if ('instance' in patch) lset('dir.instance', state.instance);
    if ('rangePreset' in patch) lset('dir.range', state.rangePreset);
    if ('tz' in patch) lset('dir.tz', state.tz);
    if ('theme' in patch) lset('dir.theme', state.theme);
    if ('refreshSec' in patch) lset('dir.refresh', String(state.refreshSec));
    if ('token' in patch) { try { state.token ? sessionStorage.setItem('dir.token', state.token) : sessionStorage.removeItem('dir.token'); } catch { /* ignore */ } }
  }
  if (notify) listeners.forEach(fn => fn(patch));
}
export const subscribe = fn => { listeners.add(fn); return () => listeners.delete(fn); };
