/**
 * Sprint tech_mobile Phase 3 — offline action queue + sync engine.
 *
 * Replaces the v1 raw-IndexedDB stub with a Dexie-backed implementation
 * (see lib/offlineDb.js for schema). Supports:
 *   - queueAction(method, url, body) → optimistic queue insert + try-now
 *     when online; pure-queue when offline.
 *   - syncNow() — drains pending entries in FIFO order; idempotency key
 *     header (`Idempotency-Key` per Stripe convention; consumed by
 *     gdx/core/middleware/idempotency.py) allows safe server-side dedup.
 *   - Triggered on `online` event AND on visibilitychange (iOS doesn't
 *     reliably fire `online` when an app comes back to foreground).
 *
 * State (refs returned to callers):
 *   isOnline      — boolean (mirrors navigator.onLine)
 *   pendingCount  — number of entries in pending status
 *   syncing       — true while drain is in flight
 *   lastSyncedAt  — ISO8601 of the last successful drain
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { db, QUEUE_STATUS, getMetadata, setMetadata } from '../lib/offlineDb'
import { useOnlineState } from './useOnlineState'

const { isOnline } = useOnlineState()
const pendingCount = ref(0)
const syncing = ref(false)
const lastSyncedAt = ref(null)

function _uuid() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  // RFC4122-ish fallback; fine for idempotency key (uniqueness, not crypto).
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

async function _refreshPendingCount() {
  pendingCount.value = await db.sync_queue
    .where('status').equals(QUEUE_STATUS.PENDING).count()
}

async function _hydrateLastSyncedAt() {
  lastSyncedAt.value = await getMetadata('last_synced_at', null)
}

/**
 * Queue a mutation. If online, attempts immediately and returns the
 * server response on success; on network failure the entry stays
 * queued and a stub `{ queued: true, idempotency_key }` resolves so
 * the UI can treat the action as optimistically applied.
 *
 * @param {string} method  'POST' | 'PATCH' | 'PUT' | 'DELETE'
 * @param {string} url
 * @param {object} [body]
 * @param {object} [opts]  { actionType, resourceId, headers }
 */
export async function queueAction(method, url, body = null, opts = {}) {
  const idempotency_key = _uuid()
  const entry = {
    idempotency_key,
    action_type: opts.actionType || `${method} ${url}`,
    resource_id: opts.resourceId || '',
    method,
    url,
    body,
    headers: opts.headers || {},
    status: QUEUE_STATUS.PENDING,
    attempt_count: 0,
    last_error: null,
    last_error_code: null,
    last_attempted_at: null,
    created_at: new Date().toISOString(),
  }

  // Insert before attempting — if the network call succeeds we'll mark
  // synced; if it fails we already have the row to retry later.
  const id = await db.sync_queue.add(entry)
  await _refreshPendingCount()

  if (isOnline.value) {
    try {
      const result = await _drainOne({ ...entry, id })
      // Successful path returns parsed JSON or null.
      return result
    } catch (e) {
      // 4xx (validation/authz) is a real answer, not an outage: the row is
      // already marked FAILED and will never be replayed, so returning the
      // "queued" stub here would tell the caller a dead request was saved.
      // Rethrow so the caller's error path runs. (409 never reaches here —
      // _drainOne treats it as synced.)
      //
      // 401 is the exception: _drainOne left it PENDING to retry, so it is an
      // outage in disguise. Rethrowing would tell the tech "could not save"
      // about a write that is queued and will land.
      if (e?.status === 401 && e.transient) return { queued: true, idempotency_key }
      if (e?.status && e.status >= 400 && e.status < 500) throw e
      // Network / 5xx: row stays pending; surface a stub so the caller
      // can finish optimistically.
      return { queued: true, idempotency_key }
    }
  }
  return { queued: true, idempotency_key }
}

/**
 * What happened to this specific queued write?
 *
 * Returns 'waiting' (still to land), 'failed' (the server rejected it and it
 * will never replay), or null (it landed, or was never queued).
 *
 * A view that optimistically renders a queued row needs this to know when to
 * stop: drop it too early and the tech's note vanishes while it is still
 * queued; drop it too late and it double-renders beside the server's copy once
 * it drains. `pendingCount` can't answer it — that count is global, so one
 * unrelated queued photo would keep a long-drained note pinned on screen.
 *
 * 'failed' is reported rather than folded into null on purpose. A dead write
 * must not just disappear — the tech wrote that note and is entitled to know it
 * didn't send. Returning a bare boolean here is what made the first version of
 * this silently delete rejected work, which is the same failure the queue
 * itself exists to prevent.
 */
export async function queuedWriteStatus(idempotencyKey) {
  if (!idempotencyKey) return null
  const row = await db.sync_queue
    .where('idempotency_key')
    .equals(idempotencyKey)
    .first()
  if (!row) return null
  // SYNCING counts as waiting: it is mid-flight, so the server list can't have
  // it yet.
  if (row.status === QUEUE_STATUS.PENDING || row.status === QUEUE_STATUS.SYNCING) return 'waiting'
  if (row.status === QUEUE_STATUS.FAILED) return 'failed'
  return null
}

async function _drainOne(entry) {
  await db.sync_queue.update(entry.id, {
    status: QUEUE_STATUS.SYNCING,
    last_attempted_at: new Date().toISOString(),
  })
  let token = null
  try {
    // Read auth at the moment of replay — token may have refreshed since
    // the action was queued.
    token = sessionStorage.getItem('gdx_access_token') || null
  } catch {}
  const tenantSlug = (() => {
    try {
      const stored = sessionStorage.getItem('gdx_tenant_slug')
      if (stored) return stored
      const parts = window.location.hostname.split('.')
      const sub = parts.length >= 3 ? parts[0] : null
      return sub && sub !== 'www' ? sub : null
    } catch { return null }
  })()
  const headers = {
    'Content-Type': 'application/json',
    // Server middleware is Stripe-shaped; bare `Idempotency-Key` is the
    // canonical header (see gdx/core/middleware/idempotency.py).
    'Idempotency-Key': entry.idempotency_key,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(tenantSlug ? { 'x-tenant-id': tenantSlug } : {}),
    ...(entry.headers || {}),
  }
  let resp
  try {
    resp = await fetch(entry.url, {
      method: entry.method,
      headers,
      body: entry.body == null ? undefined : JSON.stringify(entry.body),
      credentials: 'include',
    })
  } catch (netErr) {
    // Network truly down — flip back to pending, increment attempts.
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.PENDING,
      attempt_count: (entry.attempt_count || 0) + 1,
      last_error: netErr.message || 'network_error',
    })
    await _refreshPendingCount()
    throw netErr
  }

  // 2xx OR 409 (server-side dedup detected the duplicate replay) → synced.
  if (resp.ok || resp.status === 409) {
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.SYNCED,
      last_error: null,
      last_error_code: resp.status,
    })
    await _refreshPendingCount()
    if (resp.status === 204) return null
    try { return await resp.json() } catch { return null }
  }

  // 401 is NOT the server's verdict on the work — it is a stale token, and
  // this queue exists precisely because writes sit for hours before replaying.
  // A tech queues "I'm here" in a dead zone, drives out an hour later, and the
  // token they had at 9am is expired by the time we replay: taking that 401 as
  // "this arrival is invalid, never retry" throws the arrival away silently.
  // Stay PENDING and let the next drain carry it — by then the app's own API
  // calls will have refreshed the token (useApi.request refreshes and retries
  // on 401; this replay path deliberately uses a bare fetch and cannot).
  //
  // usePhotoQueue draws the same line for the same reason (_PERMANENT excludes
  // 401). This queue — which carries arrivals, notes and part requests — did
  // not, so every one of them was one expired token away from vanishing.
  if (resp.status === 401) {
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.PENDING,
      attempt_count: (entry.attempt_count || 0) + 1,
      last_error: 'unauthorized_will_retry',
      last_error_code: 401,
    })
    await _refreshPendingCount()
    const e = new Error('unauthorized_will_retry')
    e.status = 401
    e.transient = true
    throw e
  }

  // Other 4xx — client error. Retrying won't help; flag as failed.
  if (resp.status >= 400 && resp.status < 500) {
    let detail = `HTTP ${resp.status}`
    let parsedBody = null
    try {
      parsedBody = await resp.json()
      detail = parsedBody.detail || parsedBody.error || detail
    } catch {}
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.FAILED,
      last_error: detail,
      last_error_code: resp.status,
      attempt_count: (entry.attempt_count || 0) + 1,
    })
    await _refreshPendingCount()
    const e = new Error(detail)
    e.status = resp.status
    // Callers key richer UX off the response body (e.g. closeout's
    // `missing` checklist) — keep parity with useApi's error shape.
    e.body = parsedBody
    throw e
  }

  // 5xx / 502 / 503 — transient; retry on next drain.
  await db.sync_queue.update(entry.id, {
    status: QUEUE_STATUS.PENDING,
    attempt_count: (entry.attempt_count || 0) + 1,
    last_error: `HTTP ${resp.status}`,
    last_error_code: resp.status,
  })
  await _refreshPendingCount()
  const e = new Error(`HTTP ${resp.status}`)
  e.status = resp.status
  throw e
}

/**
 * Drain all pending entries (FIFO). Stops on first network error so we
 * don't churn the queue when the network is genuinely down. Idempotent:
 * if called while a drain is already in flight, the subsequent call is
 * a no-op.
 */
export async function syncNow() {
  if (syncing.value) return
  if (!isOnline.value) return
  syncing.value = true
  try {
    const pending = await db.sync_queue
      .where('status').equals(QUEUE_STATUS.PENDING)
      .sortBy('created_at')
    for (const entry of pending) {
      try {
        await _drainOne(entry)
      } catch (e) {
        // Network or 5xx — bail; queue stays for next attempt.
        if (!e?.status || e.status >= 500) break
        // 4xx — entry was already marked failed; continue with next.
      }
    }
    const stamp = new Date().toISOString()
    await setMetadata('last_synced_at', stamp)
    lastSyncedAt.value = stamp
  } finally {
    syncing.value = false
    await _refreshPendingCount()
  }
}

/**
 * Purge entries that have been in 'synced' status for longer than the
 * given window (default 7 days). Called opportunistically — the queue
 * stays clean over time but doesn't churn on every call.
 */
export async function purgeOldSynced(maxAgeDays = 7) {
  const cutoff = new Date(Date.now() - maxAgeDays * 86400_000).toISOString()
  await db.sync_queue
    .where('status').equals(QUEUE_STATUS.SYNCED)
    .filter(e => e.last_attempted_at && e.last_attempted_at < cutoff)
    .delete()
}

/**
 * Composable wrapper — installs window/visibility listeners that auto-
 * sync, exposes the reactive state. Safe to call from many components;
 * the underlying store is a singleton.
 */
export function useOfflineSync() {
  let onlineHandler
  let visibilityHandler

  onMounted(async () => {
    await _refreshPendingCount()
    await _hydrateLastSyncedAt()
    onlineHandler = () => { syncNow() }
    visibilityHandler = () => {
      if (!document.hidden && isOnline.value) syncNow()
    }
    window.addEventListener('online', onlineHandler)
    document.addEventListener('visibilitychange', visibilityHandler)
    // If we landed online with a pending queue (e.g. tab restore), drain.
    if (isOnline.value) {
      // Defer a tick so other onMounted hooks finish first.
      Promise.resolve().then(() => syncNow())
    }
  })

  onUnmounted(() => {
    if (onlineHandler) window.removeEventListener('online', onlineHandler)
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler)
  })

  return {
    isOnline,
    pendingCount,
    syncing,
    lastSyncedAt,
    queueAction,
    syncNow,
    purgeOldSynced,
  }
}
