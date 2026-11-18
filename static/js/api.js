import { state } from './state.js';

export class ApiError extends Error {
  constructor(status, body) {
    const e = (body && body.error) || {};
    super(e.message || (body && body.detail) || `Request failed (${status})`);
    this.status = status; this.code = e.code || 'HTTP_ERROR'; this.hint = e.hint; this.details = e.details; this.requestId = e.request_id;
  }
}
let authPrompt = null;
export const onAuthNeeded = fn => { authPrompt = fn; };

export async function api(path, { method = 'GET', params, body, signal } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let res;
  try { res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, signal }); }
  catch (e) { if (e.name === 'AbortError') throw e; throw new ApiError(0, { error: { code: 'NETWORK', message: 'Cannot reach the DB Incident Replay server.', hint: 'Is the server running?' } }); }
  const ctype = res.headers.get('content-type') || '';
  const data = ctype.includes('json') ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) {
    if (res.status === 401 && authPrompt) authPrompt();
    throw new ApiError(res.status, typeof data === 'object' ? data : { detail: String(data).slice(0, 200) });
  }
  return data;
}
export const inst = (extra = {}) => ({ instance: state.instance || undefined, ...extra });
export const get = (path, params) => api(path, { params });
export const post = (path, body, params) => api(path, { method: 'POST', body: body ?? {}, params });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
export const del = (path, params) => api(path, { method: 'DELETE', params });
