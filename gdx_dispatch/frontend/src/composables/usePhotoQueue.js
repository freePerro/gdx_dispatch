/**
 * Offline-capable job photo capture.
 *
 * A tech photographs a door inside a garage, in a rural driveway, behind a
 * building — the dead zones ARE the use case. So a photo is written to
 * IndexedDB first and uploaded when there's signal, never dropped because the
 * bars were missing at the moment of the tap.
 *
 * Why this doesn't ride `postQueued` like every other mobile write:
 * `useOfflineSync._drainOne` hardcodes `Content-Type: application/json` and
 * `JSON.stringify(entry.body)`, so the JSON queue physically cannot carry a
 * file. That's a limit of that one function, not of the offline layer — the
 * `photos` store has been in the Dexie schema since Sprint 3 ("captured photo
 * blobs awaiting upload… blob stored as Blob, no base64 overhead") with zero
 * writers and zero readers. This is the writer it was waiting for.
 *
 * Drains on the same signals as the JSON queue (`online` + `visibilitychange`
 * — iOS never fires a reliable `online`), so a photo taken in a basement lands
 * when the tech gets back to the truck.
 *
 * Posts to the SAME route the desktop uses — `/api/jobs/{id}/photos` — not the
 * mobile-only twin. Doug: "if they are on a job it should just automatically be
 * tagged to that job and that customer", and the job already knows its
 * customer, so the URL is the tagging. One route means the office sees a tech's
 * photo on the very same Photos page as their own; two routes is how you get a
 * photo nobody can find.
 */
import { ref } from 'vue'
import { db, QUEUE_STATUS } from '../lib/offlineDb'
import { createApiClient } from './useApi'

const log = { error: (...a) => { try { console.error(...a) } catch { /* noop */ } } }

const pendingPhotos = ref(0)
const uploadingPhotos = ref(false)
let wired = false

function _uuid() {
  try {
    if (crypto?.randomUUID) return crypto.randomUUID()
  } catch { /* older webview */ }
  return `p-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function _refreshPendingPhotos() {
  try {
    pendingPhotos.value = await db.photos
      .where('status').equals(QUEUE_STATUS.PENDING).count()
  } catch {
    pendingPhotos.value = 0
  }
}

// A phone photo is 3-12MB. Quota eviction is all-or-nothing per origin, so an
// unbounded backlog doesn't just lose photos — it takes `sync_queue` with it,
// and that holds the tech's unsynced closeouts and payments. Bound the thing
// that actually grows: blobs still waiting to upload. Synced rows already had
// their blob dropped, so pruning those reclaims nothing.
const MAX_PENDING_PHOTOS = 40

async function _pruneStore() {
  try {
    // Synced rows are metadata only — keep a few so the UI can say "sent".
    const done = await db.photos.where('status').equals(QUEUE_STATUS.SYNCED).sortBy('created_at')
    for (const r of done.slice(0, Math.max(0, done.length - 10))) {
      await db.photos.delete(r.id)
    }
  } catch { /* opportunistic; never block a capture */ }
}

async function _pendingCount() {
  try {
    return await db.photos.where('status').equals(QUEUE_STATUS.PENDING).count()
  } catch {
    return 0
  }
}

/**
 * Store a captured photo and try to send it now.
 *
 * Returns { queued: true } when it's saved locally but not yet uploaded — the
 * caller must say so rather than claim success.
 */
export async function capturePhoto(jobId, blob, kind = null) {
  await _pruneStore()
  if ((await _pendingCount()) >= MAX_PENDING_PHOTOS) {
    // Refuse rather than silently push the origin over quota and take the
    // tech's unsynced closeouts down with it. The caller surfaces this.
    const err = new Error('Too many photos still waiting to upload')
    err.code = 'photo_backlog_full'
    throw err
  }
  const id = _uuid()
  await db.photos.put({
    id,
    job_id: String(jobId),
    // Dexie stores a Blob as a non-indexed property directly — no base64,
    // which would inflate a 3 MB phone photo by ~33% for no reason.
    blob,
    kind: kind || null,
    filename: blob?.name || 'photo.jpg',
    content_type: blob?.type || 'image/jpeg',
    status: QUEUE_STATUS.PENDING,
    attempts: 0,
    created_at: new Date().toISOString(),
  })
  await _refreshPendingPhotos()

  if (!navigator.onLine) return { queued: true, id }
  const sent = await _uploadOne(id)
  return sent ? { queued: false, id } : { queued: true, id }
}

// The server will never accept these, however many times we ask: a bad slot,
// a non-image, a file over the limit, a job that isn't this tech's. Anything
// else — 401 above all — is transient and MUST be retried.
const _PERMANENT = new Set([400, 404, 413, 415, 422])

/** Does this throw look like the network, rather than broken code?
 *  `fetch` signals a dropped connection with a TypeError; a missing dependency
 *  or a coding mistake surfaces as anything else. */
function _looksLikeNetwork(err) {
  if (err instanceof TypeError) return true
  return /network|failed to fetch|load failed|connection|timeout/i.test(err?.message || '')
}

// In-flight guard. `online` and `visibilitychange` routinely fire together on
// reconnect, and a capture can upload while a drain is walking the same row —
// without this the same photo posts twice and the job gets a duplicate.
const _inFlight = new Set()

async function _uploadOne(photoId) {
  // Claim BEFORE the first await. Checking then awaiting then adding leaves a
  // window where two callers both pass the check and both POST — and the
  // normal path races: opening the camera hides the document, so returning
  // fires visibilitychange -> drainPhotos at the same moment `change` ->
  // capturePhoto uploads the row just written. The server INSERTs a fresh uuid
  // every time, so a duplicate is permanent.
  if (_inFlight.has(photoId)) return false
  _inFlight.add(photoId)
  try {
    const row = await db.photos.get(photoId)
    if (!row || row.status === QUEUE_STATUS.SYNCED) return true
    return await _sendPhoto(photoId, row)
  } finally {
    _inFlight.delete(photoId)
  }
}

async function _sendPhoto(photoId, row) {

  const form = new FormData()
  form.append('file', row.blob, row.filename || 'photo.jpg')
  if (row.kind) form.append('kind', row.kind)

  try {
    // Through the shared client, not a bare fetch: it already detects FormData
    // (and leaves Content-Type alone so the browser sets the multipart
    // boundary), adds the tenant header, and — the reason this matters — on a
    // 401 it refreshes the token and retries. A photo captured at 9am and
    // drained at 5pm meets an expired token; hand-rolled fetch would take that
    // 401 as the server's verdict on the photo.
    await createApiClient().post(`/api/jobs/${row.job_id}/photos`, form)
  } catch (err) {
    const status = err?.status || 0
    if (!status && !_looksLikeNetwork(err)) {
      // A throw carrying no HTTP status and not shaped like a network failure
      // is a BUG — a broken client, a store failure, a missing dependency.
      // Filing it under "transient, we'll retry" is how a tech ends up staring
      // at "uploads when you have signal" on full bars while nothing ever
      // uploads: precisely the silent shape of 0 photos across 205 jobs,
      // reproduced one layer up. Fail loud instead.
      //
      // Note this deliberately does NOT key off navigator.onLine — it lies
      // (true on a router with no internet), and a real drop throws
      // TypeError: Failed to fetch with onLine still true.
      await db.photos.update(photoId, {
        status: QUEUE_STATUS.FAILED,
        error: err?.message || 'upload failed',
        last_attempted_at: new Date().toISOString(),
      })
      await _refreshPendingPhotos()
      log.error('photo_upload_broken', err)
      return false
    }
    if (_PERMANENT.has(status)) {
      // Keep the blob. The tech was told this photo was saved, and a rejected
      // upload is not a reason to destroy the only copy of a door they already
      // drove away from — it's a reason to tell someone.
      await db.photos.update(photoId, {
        status: QUEUE_STATUS.FAILED,
        error: `HTTP ${status}`,
        last_attempted_at: new Date().toISOString(),
      })
      await _refreshPendingPhotos()
      return false
    }
    // Offline, 401-after-refresh-failed, 5xx, flaky signal: keep it pending.
    await db.photos.update(photoId, {
      attempts: (row.attempts || 0) + 1,
      last_attempted_at: new Date().toISOString(),
    })
    return false
  }

  // Landed. Drop the blob now — a truck's worth of 8MB photos would otherwise
  // push the origin past its quota, and eviction takes the WHOLE database,
  // including the sync_queue holding the tech's unsynced closeouts.
  await db.photos.update(photoId, {
    status: QUEUE_STATUS.SYNCED,
    blob: null,
    synced_at: new Date().toISOString(),
  })
  await _refreshPendingPhotos()
  return true
}

/** Upload every stored photo. Safe to call repeatedly. */
export async function drainPhotos() {
  if (uploadingPhotos.value || !navigator.onLine) return
  uploadingPhotos.value = true
  try {
    const pending = await db.photos
      .where('status').equals(QUEUE_STATUS.PENDING)
      .sortBy('created_at')
    for (const row of pending) {
      const ok = await _uploadOne(row.id)
      // Bail on the first network failure — the rest will fail too, and
      // hammering a dead connection just burns the tech's battery.
      if (!ok) {
        const after = await db.photos.get(row.id)
        if (after?.status === QUEUE_STATUS.PENDING) break
      }
    }
  } finally {
    uploadingPhotos.value = false
    await _refreshPendingPhotos()
  }
}

export function usePhotoQueue() {
  if (!wired) {
    wired = true
    _refreshPendingPhotos()
    try {
      window.addEventListener('online', drainPhotos)
      // iOS Safari doesn't reliably fire `online`; coming back to the tab is
      // the signal that actually happens.
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) drainPhotos()
      })
    } catch { /* SSR / test env */ }
  }
  return { pendingPhotos, uploadingPhotos, capturePhoto, drainPhotos }
}
