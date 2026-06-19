// Unified tour engine — wraps driver.js, syncs progress to the server.
//
// Replaces useMobileTour.js piecemeal during Slice 3. Existing callers
// keep working until each role tour is ported into tours/catalog.js.
//
// Progress lifecycle:
//   1. On first call, GET /api/me/tours → cache progress in module state.
//   2. autoLaunchForUser({ role, modules }) inspects the catalog +
//      progress and starts the matching role tour iff no progress row
//      exists (or its version is older than the catalog's).
//   3. launch(tourId, { force }) bypasses progress checks (replay).
//   4. Each step change POSTs /step. completion POSTs /complete. skip
//      POSTs /skip. Failures fall back to localStorage and log a
//      breadcrumb — never blocks the user.

import { driver } from 'driver.js';
import 'driver.js/dist/driver.css';
import { findTour, toursForRole, defaultTourIdForRole } from '../tours/catalog';

const LS_PROGRESS_KEY = 'gdx_tour_progress_v1';
const API_BASE = '/api/me/tours';

let _progressCache = null;
let _activeDriver = null;
let _activeClickHandler = null;

// Drop cached progress + localStorage on logout so the next user on this
// browser doesn't inherit the previous user's tour state. Auth store
// dispatches `gdx:auth-logout` from its logout() — wire here at module
// load (singleton, runs exactly once per SPA boot).
if (typeof window !== 'undefined') {
  window.addEventListener('gdx:auth-logout', () => {
    _progressCache = null;
    try { localStorage.removeItem(LS_PROGRESS_KEY); } catch { /* swallow */ }
  });
}

function _isMobile() {
  return typeof window !== 'undefined' && window.innerWidth < 600;
}

function _readLocalProgress() {
  try {
    const raw = localStorage.getItem(LS_PROGRESS_KEY);
    if (!raw) return {};
    return JSON.parse(raw);
  } catch { return {}; }
}

function _writeLocalProgress(progress) {
  try { localStorage.setItem(LS_PROGRESS_KEY, JSON.stringify(progress)); } catch { /* swallow */ }
}

function _authHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  try {
    const token = sessionStorage.getItem('gdx_access_token');
    if (token) headers.Authorization = `Bearer ${token}`;
    const tenantSlug = sessionStorage.getItem('gdx_tenant_slug');
    if (tenantSlug) headers['X-Tenant'] = tenantSlug;
  } catch { /* swallow */ }
  return headers;
}

async function _apiCall(path, opts = {}) {
  try {
    const r = await fetch(`${API_BASE}${path}`, {
      method: opts.method || 'GET',
      credentials: 'include',
      headers: _authHeaders(),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function _ensureProgressLoaded() {
  if (_progressCache !== null) return _progressCache;
  const remote = await _apiCall('');
  if (remote && remote.available) {
    _progressCache = remote.progress || {};
  } else {
    _progressCache = _readLocalProgress();
  }
  return _progressCache;
}

function _hasCompleted(tourId, catalogVersion) {
  if (!_progressCache) return false;
  const row = _progressCache[tourId];
  if (!row) return false;
  if (row.version < catalogVersion) return false; // catalog bumped → re-fire
  return row.status === 'completed' || row.status === 'skipped';
}

function _trackEvent(name, payload) {
  try {
    window.dispatchEvent(new CustomEvent('gdx:analytics', { detail: { name, payload } }));
  } catch { /* swallow */ }
}

function _resolveSteps(tour) {
  const usableSteps = [];
  for (const s of tour.steps) {
    if (!s.anchor) continue;
    let exists = true;
    if (typeof document !== 'undefined') {
      try { exists = !!document.querySelector(s.anchor); } catch { exists = false; }
    }
    if (!exists) continue;
    usableSteps.push({
      element: s.anchor,
      popover: {
        title: s.title,
        description: s.description + (s.helpArticleId ? '\n\n[Learn more]' : ''),
        // driver.js click handlers are wired via onPopoverRender below.
      },
      _meta: s, // stash for the render callback
    });
  }
  return usableSteps;
}

async function _persist(tourId, action, body = null) {
  const path = `/${encodeURIComponent(tourId)}/${action}`;
  const result = await _apiCall(path, { method: 'POST', body });
  // Mirror to localStorage regardless — first device of a session may
  // have offline'd before the server got the update. Preserve the
  // catalog version in the local fallback so version-bump re-fire still
  // works when only localStorage is available.
  const local = _readLocalProgress();
  const fallback = {
    ...(local[tourId] || {}),
    status: action === 'step' ? (local[tourId]?.status || 'started') : action,
  };
  if (body && body.version != null) fallback.version = body.version;
  local[tourId] = result || fallback;
  _writeLocalProgress(local);
  if (_progressCache) _progressCache[tourId] = local[tourId];
  return result;
}

function _runTour(tour) {
  // Idempotency: if a tour is already running, ignore a second invocation
  // rather than stacking listeners + drivers. The user can dismiss the
  // active one first.
  if (_activeDriver) return;

  const steps = _resolveSteps(tour);
  if (steps.length === 0) {
    _trackEvent('tour_skipped_no_anchors', { tour_id: tour.id });
    return;
  }

  let currentIndex = 0;
  const helpArticleHandler = (e) => {
    if (e.target?.dataset?.tourLearnMore) {
      const slug = e.target.dataset.tourLearnMore;
      window.dispatchEvent(new CustomEvent('gdx:help-open', { detail: { slug, source: 'tour_step' } }));
    }
  };
  _activeClickHandler = helpArticleHandler;

  _activeDriver = driver({
    showProgress: true,
    animate: true,
    popoverClass: _isMobile() ? 'gdx-tour-mobile' : 'gdx-tour-desktop',
    stagePadding: 4,
    onPopoverRender: (popover, { state }) => {
      const step = state.activeStep?._meta;
      currentIndex = state.activeIndex || 0;
      if (step?.helpArticleId) {
        const link = document.createElement('button');
        link.type = 'button';
        link.className = 'gdx-tour-learn-more';
        link.textContent = 'Learn more';
        link.dataset.tourLearnMore = step.helpArticleId;
        popover.footerButtons?.appendChild(link);
      }
      _persist(tour.id, 'step', { step_index: currentIndex });
      _trackEvent('tour_step', { tour_id: tour.id, step_index: currentIndex });
    },
    onDestroyStarted: () => {
      const finished = currentIndex >= steps.length - 1;
      if (finished) {
        _persist(tour.id, 'complete', { version: tour.version });
        _trackEvent('tour_completed', { tour_id: tour.id });
      } else {
        _persist(tour.id, 'skip', { version: tour.version });
        _trackEvent('tour_skipped', { tour_id: tour.id, last_step: currentIndex });
      }
      _activeDriver?.destroy();
      _activeDriver = null;
      if (_activeClickHandler) {
        document.removeEventListener('click', _activeClickHandler);
        _activeClickHandler = null;
      }
    },
    steps,
  });

  // Send the catalog version on start so the server row gets refreshed
  // when we bump versions on a tour rewrite. Without this, _hasCompleted's
  // version comparison would loop forever.
  _persist(tour.id, 'start', { version: tour.version });
  _trackEvent('tour_started', { tour_id: tour.id, total_steps: steps.length });
  document.addEventListener('click', helpArticleHandler);
  _activeDriver.drive();
}

export function useTour() {
  async function launch(tourId, { force = false } = {}) {
    const tour = findTour(tourId);
    if (!tour) return false;
    await _ensureProgressLoaded();
    if (!force && _hasCompleted(tourId, tour.version)) return false;
    _runTour(tour);
    return true;
  }

  async function autoLaunchForUser({ role, modules = null }) {
    if (!role) return false;
    // Synchronous re-entry guard — if a tour is already on-screen, never
    // try to auto-launch another. Belt + braces with _runTour's own
    // _activeDriver check, but stops two awaiting autoLaunch calls from
    // both passing through (the gap between await + driver-instantiation
    // is the window).
    if (_activeDriver) return false;
    await _ensureProgressLoaded();
    if (_activeDriver) return false; // re-check after the await
    const candidates = toursForRole(role, { enabledModules: modules });
    for (const tour of candidates) {
      if (!tour.autoLaunch) continue;
      if (_hasCompleted(tour.id, tour.version)) continue;
      _runTour(tour);
      return true;
    }
    return false;
  }

  function defaultTourFor(role) {
    return defaultTourIdForRole(role);
  }

  function stop() {
    try { _activeDriver?.destroy(); } catch { /* swallow */ }
    _activeDriver = null;
  }

  function _resetCache() {
    _progressCache = null;
  }

  return { launch, autoLaunchForUser, defaultTourFor, stop, _resetCache };
}
