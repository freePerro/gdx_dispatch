// Lightweight client telemetry for help drawer + tours.
//
// Listens for `gdx:analytics` CustomEvents emitted by the help store and
// the tour engine. Each event is debounced + posted to the existing
// audit endpoint (`/api/audit/ux-event`) if it exists, with a Sentry
// breadcrumb fallback so we always have something in the logs.
//
// Why a separate file: keeps the tour and help store decoupled from
// transport concerns. Replace the backend POST with whatever your
// preferred sink is (Mixpanel, PostHog, etc.) by editing this file
// only.

const QUEUE_KEY = 'gdx_ux_event_queue_v1';
const FLUSH_INTERVAL_MS = 4000;
const MAX_QUEUE = 50;
const ENDPOINT = '/api/audit/ux-event';

let _installed = false;
let _flushTimer = null;
let _endpointAvailable = true;

function _readQueue() {
  try {
    const raw = localStorage.getItem(QUEUE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function _writeQueue(q) {
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(q.slice(-MAX_QUEUE)));
  } catch { /* swallow */ }
}

function _enqueue(event) {
  const q = _readQueue();
  q.push({ ...event, ts: new Date().toISOString() });
  _writeQueue(q);
  _scheduleFlush();
}

function _breadcrumb(event) {
  try {
    const sentry = window?.Sentry || window?.sentry;
    if (sentry?.addBreadcrumb) {
      sentry.addBreadcrumb({
        category: 'ux',
        message: event.name,
        level: 'info',
        data: event.payload || {},
      });
    }
  } catch { /* swallow */ }
}

function _authHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  try {
    const token = sessionStorage.getItem('gdx_access_token');
    if (token) headers.Authorization = `Bearer ${token}`;
    const slug = sessionStorage.getItem('gdx_tenant_slug');
    if (slug) headers['X-Tenant'] = slug;
  } catch { /* swallow */ }
  return headers;
}

async function _flush() {
  _flushTimer = null;
  if (!_endpointAvailable) return;
  const q = _readQueue();
  if (q.length === 0) return;
  // Wait until login lands a token; without one, the endpoint 401s and
  // we'd waste the queue. Defer flush by one tick — analytics events
  // that fire pre-login are kept queued for the next flush.
  const headers = _authHeaders();
  if (!headers.Authorization) {
    _scheduleFlush();
    return;
  }
  try {
    const r = await fetch(ENDPOINT, {
      method: 'POST',
      credentials: 'include',
      headers,
      body: JSON.stringify({ events: q }),
    });
    if (r.status === 404 || r.status === 405) {
      // Endpoint not deployed yet (404) or route not wired for POST (405).
      // Not coming back this page load — stop trying.
      _endpointAvailable = false;
      try { localStorage.removeItem(QUEUE_KEY); } catch { /* swallow */ }
      return;
    }
    if (r.status === 401 || r.status === 403) {
      // Token expired or insufficient role. Drop the queue (don't
      // accumulate for a session that might never re-auth) and stop
      // trying this page load. The next page load + login will
      // re-install us with a fresh auth context.
      _endpointAvailable = false;
      try { localStorage.removeItem(QUEUE_KEY); } catch { /* swallow */ }
      return;
    }
    if (r.ok) {
      // Successful flush — clear queue.
      try { localStorage.removeItem(QUEUE_KEY); } catch { /* swallow */ }
    }
  } catch {
    // Network error — keep queue; next event will retrigger flush.
  }
}

function _scheduleFlush() {
  if (_flushTimer) return;
  _flushTimer = window.setTimeout(_flush, FLUSH_INTERVAL_MS);
}

function _handleEvent(e) {
  const detail = e?.detail || {};
  if (!detail.name) return;
  _breadcrumb(detail);
  _enqueue(detail);
}

export function installAnalytics() {
  if (_installed) return;
  _installed = true;
  window.addEventListener('gdx:analytics', _handleEvent);
  // Flush on page hide so we don't lose events on tab close.
  window.addEventListener('pagehide', _flush, { capture: true });
  window.addEventListener('beforeunload', _flush, { capture: true });
}

export function _resetForTests() {
  _installed = false;
  _flushTimer = null;
  _endpointAvailable = true;
  try { localStorage.removeItem(QUEUE_KEY); } catch { /* swallow */ }
}
