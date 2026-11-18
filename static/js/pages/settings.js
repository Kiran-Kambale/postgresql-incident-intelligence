// Monitoring Settings: thresholds, intervals and retention (admin). Security settings are environment-only and shown read-only.
import { get, put, del } from '../api.js';
import { h, badge, toast, errorBox, callout, kv, confirmDialog, icon } from '../ui.js';
import { pageHead, refreshButton } from '../common.js';

export async function render(ctx) {
  const root = h('div'); const body = h('div'); const dirty = new Map();
  root.append(pageHead('Monitoring Settings', 'Collection intervals, detection thresholds and retention. Changes apply immediately and are recorded in the audit log.', refreshButton(load)), body);
  async function load() {
    dirty.clear();
    try {
      const d = await get('/api/settings'); const groups = {}; d.items.forEach(i => { (groups[i.group] ||= []).push(i); });
      const saveBtn = h('button', { class: 'btn primary', type: 'button', disabled: true, onclick: save }, 'Save changes');
      const err = h('div', { class: 'callout crit', hidden: true, role: 'alert' });
      async function save() {
        err.hidden = true;
        try { const values = Object.fromEntries([...dirty.entries()]); await put('/api/settings', { values }); toast('Settings saved', 'ok'); load(); }
        catch (e) { err.hidden = false; err.replaceChildren(h('strong', e.message), e.details && !Array.isArray(e.details) ? h('ul', Object.entries(e.details).map(([k, v]) => h('li', `${k}: ${v}`))) : ''); }
      }
      const field = i => {
        const inp = h('input', { type: 'number', value: i.value, min: i.min, max: i.max, step: i.type === 'int' ? 1 : 'any', 'aria-label': i.description, style: { width: '140px' } });
        inp.addEventListener('input', () => { const v = Number(inp.value); if (inp.value === '' || v === i.value) dirty.delete(i.name); else dirty.set(i.name, v); saveBtn.disabled = dirty.size === 0; });
        return h('div', { class: 'row', style: { padding: '6px 0', borderBottom: '1px solid var(--line)' } }, h('div', { style: { flex: 1, minWidth: '240px' } }, h('div', i.description), h('div', { class: 'muted small mono' }, `${i.name} · default ${i.default} · allowed ${i.min}–${i.max}`)), inp,
          i.overridden ? h('span', { class: 'row' }, badge('changed', 'info'), h('button', { class: 'btn small', type: 'button', onclick: async () => { await del(`/api/settings/${i.name}`); toast('Reset to default', 'ok'); load(); } }, 'Reset')) : null);
      };
      body.replaceChildren(callout('', 'Intervals control how often the flight recorder collects. Thresholds change what becomes evidence and its severity; they never change what is collected.'), err,
        ...Object.entries(groups).map(([g, items]) => h('div', { class: 'card mt' }, h('h2', g), items.map(field))), h('div', { class: 'row mt' }, saveBtn),
        h('div', { class: 'card mt' }, h('h2', 'Security and connection settings (read-only)'), h('p', { class: 'muted small' }, 'These are set in the server environment (.env) and cannot be changed from the browser.'),
          kv([['Authentication', d.security.auth_mode], ['Query text mode', d.security.query_text_mode], ['Statement timeout', `${d.security.statement_timeout_ms} ms`], ['Connect timeout', `${d.security.connect_timeout_s} s`], ['Concurrent collections', d.security.max_concurrent_collections],
            ...Object.entries(d.security.tls).map(([id, t]) => [`TLS (${id})`, `${t.sslmode}${t.verified ? ' · certificate verified' : ' · certificate NOT verified'}`])])), h('p', { class: 'muted small' }, d.note));
    } catch (e) { body.replaceChildren(errorBox(e, load)); }
  }
  ctx.tick = null; await load(); return root;
}
