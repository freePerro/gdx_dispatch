// Release notes — the What's new page, its post-update banner, and the
// admin update-available notice.
//
// Notes ship inside the image (GET /api/release-notes, any signed-in user).
// "Seen" reuses the per-user tour progress rows (/api/me/tours) under the id
// `release-notes-v1-146-0`, so it follows the person across devices and
// needs no new table. A user has seen the current notes when any
// release-notes row at or above the installed version is completed or
// skipped — so rolling back to an older image never re-announces notes the
// user already read on the newer one.
//
// The update-available check calls GitHub through the server
// (GET /api/admin/update-check, settings.read). It runs at most once per
// browser session per user, and only for people who hold settings.read.

import { ref } from 'vue';
import { createApiClient } from './useApi';

const SEEN_PREFIX = 'release-notes-v';
const SEEN_RE = /^release-notes-v(\d+)-(\d+)-(\d+)$/;
const LS_SEEN_KEY = 'gdx_release_notes_seen_v1';
const SS_UPDATE_KEY = 'gdx_update_check_v1';

let _notesPromise = null;
const updateInfo = ref(null);
let _updatePromise = null;

if (typeof window !== 'undefined') {
  window.addEventListener('gdx:auth-logout', () => {
    _notesPromise = null;
    _updatePromise = null;
    updateInfo.value = null;
    try { sessionStorage.removeItem(SS_UPDATE_KEY); } catch { /* swallow */ }
    // The local seen copy is this user's, not this device's: on a shared
    // office PC or tablet the next person must still get their banner.
    try { localStorage.removeItem(LS_SEEN_KEY); } catch { /* swallow */ }
  });
}

export function parseVersion(v) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(v || '').trim());
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

export function compareVersions(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}

export function seenTourId(version) {
  return `${SEEN_PREFIX}${version.replace(/\./g, '-')}`;
}

/**
 * True when a completed/skipped release-notes row covers ``installed``.
 * ``progress`` is the /api/me/tours ``progress`` map (tour_id → row).
 */
export function hasSeenRelease(progress, installed) {
  const want = parseVersion(installed);
  if (!want) return true;
  return Object.entries(progress || {}).some(([id, row]) => {
    const m = SEEN_RE.exec(id);
    if (!m || !row || !['completed', 'skipped'].includes(row.status)) return false;
    return compareVersions([Number(m[1]), Number(m[2]), Number(m[3])], want) >= 0;
  });
}

/** GET /api/release-notes once per page load; ``{installed_version, releases}``. */
export function loadReleaseNotes({ force = false } = {}) {
  if (force || !_notesPromise) {
    const api = createApiClient();
    _notesPromise = api.get('/api/release-notes').catch((err) => {
      _notesPromise = null;
      throw err;
    });
  }
  return _notesPromise;
}

function _readLocalSeen() {
  try { return JSON.parse(localStorage.getItem(LS_SEEN_KEY) || '{}') || {}; } catch { return {}; }
}

/**
 * The tour-progress map merged over this device's local copy. The local copy
 * counts even when the server answers: a dismiss whose POST failed (a tech
 * with no signal) must not bring the banner back on every load.
 */
export async function loadSeenProgress() {
  const local = _readLocalSeen();
  try {
    const r = await createApiClient().get('/api/me/tours');
    if (r && r.available) return { ...local, ...(r.progress || {}) };
  } catch { /* the local copy alone */ }
  return local;
}

/** Record that the user has read (or dismissed) the notes for ``version``. */
export async function markReleaseSeen(version) {
  const id = seenTourId(version);
  const local = _readLocalSeen();
  local[id] = { status: 'completed' };
  try { localStorage.setItem(LS_SEEN_KEY, JSON.stringify(local)); } catch { /* swallow */ }
  try {
    await createApiClient().post(`/api/me/tours/${id}/complete`, {});
  } catch { /* the local copy still hides the banner on this device */ }
}

/**
 * The admin update-available notice. ``auth`` is the auth store; without
 * settings.read this never calls the server.
 */
export function useUpdateCheck(auth) {
  async function load() {
    if (!auth.permissionsLoaded) {
      try { await auth.loadPermissions(); } catch { /* treat as no permission */ }
    }
    if (!auth.hasPermission('settings.read')) return null;
    if (updateInfo.value) return updateInfo.value;
    try {
      const cached = JSON.parse(sessionStorage.getItem(SS_UPDATE_KEY) || 'null');
      if (cached && typeof cached === 'object') {
        updateInfo.value = cached;
        return cached;
      }
    } catch { /* swallow */ }
    if (!_updatePromise) {
      _updatePromise = createApiClient().get('/api/admin/update-check')
        .then((r) => {
          updateInfo.value = r;
          try { sessionStorage.setItem(SS_UPDATE_KEY, JSON.stringify(r)); } catch { /* swallow */ }
          return r;
        })
        .catch(() => {
          _updatePromise = null;
          return null;
        });
    }
    return _updatePromise;
  }
  return { updateInfo, loadUpdateCheck: load };
}
