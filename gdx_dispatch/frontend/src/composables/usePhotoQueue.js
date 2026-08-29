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
 * Posts to `POST /api/documents` — the ONE upload path that is actually proven
 * (1078 files live in it and download fine), takes job_id + customer_id, and
 * stores flat where the download route looks. The two job-photo-specific routes
 * were both broken: one is shadowed and 422s, the other wrote to a nested path
 * the download route never reads, so its files 404'd. Riding the proven road
 * beats a third parallel one.
 *
 * Doug: "if they are on a job it should just automatically be tagged to that
 * job and that customer" — the job knows its customer, so job_id is the tagging.
 */
import { ref } from 'vue'
import { db, QUEUE_STATUS } from '../lib/offlineDb'
import { createApiClient } from './useApi'

const log = { error: (...a) => { try { console.error(...a) } catch { /* noop */ } } }

const pendingPhotos = ref(0)
// Photos the server refused for good (or that a broken client could not send).
// Before 2026-08-28 nothing read a FAILED row: the tech was told "saved on
// your phone — uploads when you have signal", the count excluded it, and the
// photo was invisible forever. This is the reader.
const failedPhotos = ref(0)
// The refused rows themselves (id, job_id, http_status, filename) — so a
// surface can show the reason and scope itself to ITS job. The count above
// is phone-wide; a strip inside job A's sheet must not offer to delete job
// B's photo.
const failedRows = ref([])
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
  try {
    const rows = await db.photos
      .where('status').equals(QUEUE_STATUS.FAILED).sortBy('created_at')
    failedPhotos.value = rows.length
    failedRows.value = rows.map((r) => ({
      id: r.id,
      job_id: String(r.job_id),
      http_status: r.http_status ?? null,
      filename: r.filename || null,
      error: r.error || null,
    }))
  } catch {
    failedPhotos.value = 0
    failedRows.value = []
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
 * caller must say so rather than claim success. Returns { failed: true,
 * status, error } when the immediate attempt was REFUSED for good — the
 * caller must say that too; "uploads when you have signal" for a photo the
 * server has already rejected is the lie this queue used to tell.
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
  if (sent) return { queued: false, id }
  const after = await db.photos.get(id)
  if (after?.status === QUEUE_STATUS.FAILED) {
    return { queued: false, failed: true, id, status: after.http_status ?? null, error: after.error || null }
  }
  return { queued: true, id }
}

/** Plain words for a refusal — what the tech can actually do about it. */
export function describePhotoRefusal(status) {
  switch (status) {
    case 403: return "You're not allowed to add photos to this job."
    // 404 is what a job lookup would say; 409 is what the server actually
    // says today — documents.job_id is a foreign key, and a vanished job
    // surfaces as an IntegrityError that the error handler maps to 409.
    case 404:
    case 409: return 'That job no longer exists on the server.'
    case 413: return 'The photo is too large for the server.'
    case 415: return 'The server only accepts image files.'
    case 400:
    case 422: return 'The server refused this photo.'
    default: return 'The upload failed on this phone — not a signal problem.'
  }
}

// The server will never accept these, however many times we ask: a bad slot,
// a non-image, a file over the limit, a job that isn't this tech's, a job the
// caller may not touch (403 — retrying an authorization verdict changes
// nothing; 401 is different: that's an expired token and the client refreshes
// it), a job that no longer exists (409 — documents.job_id is a foreign key
// and the error handler maps the IntegrityError to 409; left out of this set
// it stayed PENDING and, because drainPhotos stops at the first row that is
// still pending, wedged every photo queued behind it). Anything else is
// transient and MUST be retried.
const _PERMANENT = new Set([400, 403, 404, 409, 413, 415, 422])

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
  // job_id is the tagging: the job knows its customer, so the photo lands on
  // both without the tech choosing anything.
  form.append('job_id', String(row.job_id))
  // Say it outright: this is a photo, not a document that happens to be a JPEG.
  form.append('as_photo', 'true')
  if (row.kind) form.append('kind', row.kind)

  try {
    // Through the shared client, not a bare fetch: it already detects FormData
    // (and leaves Content-Type alone so the browser sets the multipart
    // boundary), adds the tenant header, and — the reason this matters — on a
    // 401 it refreshes the token and retries. A photo captured at 9am and
    // drained at 5pm meets an expired token; hand-rolled fetch would take that
    // 401 as the server's verdict on the photo.
    await createApiClient().post('/api/documents', form)
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
        http_status: null,
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
        http_status: status,
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

/**
 * Put every refused photo back in line and drain. For the cases a retry can
 * fix — the job was reassigned to this tech since, the office fixed a
 * permission, the app was updated past a client bug. A photo refused for
 * size or type will simply fail again, and the strip says so again.
 */
export async function retryFailedPhotos({ jobId = null } = {}) {
  let rows = []
  try {
    rows = (await db.photos.where('status').equals(QUEUE_STATUS.FAILED).sortBy('created_at'))
      .filter((r) => !jobId || String(r.job_id) === String(jobId))
    for (const r of rows) {
      await db.photos.update(r.id, {
        status: QUEUE_STATUS.PENDING, attempts: 0, error: null, http_status: null,
      })
    }
  } catch { /* store unavailable — nothing to retry */ }
  await _refreshPendingPhotos()
  // A drain may already be walking the store — coming back from the camera
  // or from a confirm dialog fires visibilitychange. drainPhotos() would
  // return at once on `uploadingPhotos` and the tech would watch Retry do
  // nothing. Let that pass finish, then drain ours.
  for (let i = 0; i < 100 && uploadingPhotos.value; i++) {
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  await drainPhotos()
  // Say what happened. A 413 will fail again exactly the same way; the tech
  // must hear that, not watch the strip blink.
  const outcome = { retried: rows.length, uploaded: 0, refused: 0, pending: 0, status: null }
  for (const r of rows) {
    let after = null
    try { after = await db.photos.get(r.id) } catch { /* treat as gone */ }
    if (!after || after.status === QUEUE_STATUS.SYNCED) outcome.uploaded += 1
    else if (after.status === QUEUE_STATUS.FAILED) {
      outcome.refused += 1
      if (outcome.status === null) outcome.status = after.http_status ?? null
    } else outcome.pending += 1
  }
  return outcome
}

/**
 * Delete every refused photo from this phone. Destructive — these blobs are
 * the ONLY copy — so callers confirm with the tech first. Never called by the
 * queue itself.
 */
export async function discardFailedPhotos({ jobId = null } = {}) {
  let rows = []
  try {
    rows = (await db.photos.where('status').equals(QUEUE_STATUS.FAILED).sortBy('created_at'))
      .filter((r) => !jobId || String(r.job_id) === String(jobId))
    for (const r of rows) await db.photos.delete(r.id)
  } catch { /* store unavailable — nothing to discard */ }
  await _refreshPendingPhotos()
  return rows.length
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
  return {
    pendingPhotos, failedPhotos, failedRows, uploadingPhotos,
    capturePhoto, drainPhotos, retryFailedPhotos, discardFailedPhotos, describePhotoRefusal,
  }
}
