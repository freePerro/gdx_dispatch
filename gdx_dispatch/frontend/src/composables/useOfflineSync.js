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
 *   failedActions — writes the server refused during a BACKGROUND replay
 *                   (#528): the tech was told "saved offline — submits
 *                   automatically", so nothing else will ever tell them it
 *                   didn't. Refusals the caller already showed are excluded.
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { db, QUEUE_STATUS, getMetadata, setMetadata } from '../lib/offlineDb'
import { useOnlineState } from './useOnlineState'

const { isOnline } = useOnlineState()
const pendingCount = ref(0)
const syncing = ref(false)
const lastSyncedAt = ref(null)
// Exported so a reader (the strip) can show it WITHOUT mounting useOfflineSync()
// — every mount of the composable starts a drain.
export const failedActions = ref([])

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
  await _refreshFailedActions()
}

// A refused row keeps its body — a closeout's parts, hours and signature — on
// the phone until the tech retries or discards it (purgeOldSynced only drops
// refusals already seen or replaced). What was missing was anything that READ
// them (#528).
export async function refreshFailedActions() {
  return _refreshFailedActions()
}

async function _refreshFailedActions() {
  try {
    const rows = await db.sync_queue
      .where('status').equals(QUEUE_STATUS.FAILED)
      .sortBy('created_at')
    failedActions.value = rows
      .filter((r) => !r.acknowledged)
      .map((r) => ({
        id: r.id,
        action_type: r.action_type,
        resource_id: r.resource_id ? String(r.resource_id) : '',
        http_status: r.last_error_code ?? null,
        error: r.last_error || null,
        // The server's machine code for the refusal, when it sent one. A
        // payment's `duplicate_payment` 409 means the money IS on the invoice —
        // the opposite of every other payment refusal — and only the code says so.
        reason: r.last_error_reason || null,
        // Recovered from a page that died mid-send, too late (or too risky) to
        // replay on its own: it may have reached the server.
        uncertain: !!r.uncertain,
        missing: Array.isArray(r.last_error_missing) ? r.last_error_missing : [],
        amount: _paymentAmount(r),
        created_at: r.created_at,
      }))
  } catch {
    failedActions.value = []
  }
}

// "Tell the office" needs something to tell them: what the payment was.
function _paymentAmount(row) {
  if (row.action_type !== 'invoice.payment') return null
  const n = Number(row.body?.amount)
  if (!Number.isFinite(n) || n <= 0) return null
  const method = typeof row.body?.method === 'string' ? row.body.method.trim() : ''
  return `$${n.toFixed(2)}${method ? ` ${method}` : ''}`
}

// The server's refusal as a sentence. `detail` is sometimes structured — a
// payment 409 carries {code, message}, FastAPI's 422 a list of field errors —
// and storing the object rendered "[object Object]" to the tech.
function _detailText(body, status) {
  const d = body?.detail ?? body?.error
  if (typeof d === 'string' && d.trim()) return d
  if (Array.isArray(d)) {
    const msgs = d.map((x) => (typeof x === 'string' ? x : x?.msg || x?.message)).filter(Boolean)
    if (msgs.length) return msgs.join('; ')
  }
  if (d && typeof d === 'object') {
    const m = d.message || d.detail || d.msg
    if (typeof m === 'string' && m.trim()) return m
  }
  return `HTTP ${status}`
}

// Writes where a newer one that LANDED makes an older one obsolete, by group:
// a later closeout of the same job; a later dispatch status (an arrival that
// landed makes an older "on my way" pointless — the server refuses a backward
// move anyway); a later Today order; a later parts status. Notes, part
// requests, chat, change orders and payments are each their own thing. An
// address fix is NOT here: each fix carries the address it expected, so the
// second depends on the first — retiring the first makes the server refuse the
// second.
//
// The rule for RETIRING is "a newer one LANDED", never "a newer one exists": a
// redo refused on the spot (say, closed out again without the customer there
// to sign) replaces nothing on the server, and retiring the signed closeout
// still waiting behind a stale token would lose the only attestation,
// silently. ORDER is a separate rule — the newest unsent one goes first, so
// the phone learns whether it landed before deciding the older (see _claim).
const SUPERSEDE_GROUP = {
  'job.closeout': 'closeout',
  'job.en_route': 'dispatch',
  'job.arrived': 'dispatch',
  'today.reorder': 'reorder',
  'parts.status': 'parts',
}

function _supersedeGroup(actionType) {
  return SUPERSEDE_GROUP[actionType] || null
}

// Is there a write of this row's group, for the same resource, inserted AFTER
// it (the auto-increment id is the one strict order — two writes in the same
// millisecond share a created_at) that is on the server? SYNCED says so; so
// does superseded — a row is only ever retired because something newer than it
// is already there, which is newer than this one too.
function _newerLanded(row, rows) {
  const group = _supersedeGroup(row.action_type)
  if (!group) return false
  return rows.some((o) => o.id > row.id
    && (o.status === QUEUE_STATUS.SYNCED || o.superseded)
    && _supersedeGroup(o.action_type) === group
    && String(o.resource_id || '') === String(row.resource_id || ''))
}

// A newer write whose outcome the phone does not know yet: still to send, or
// "may have sent" (an answer the phone never got, shown to the tech to check).
// Either may be on the server already, so an older one waits for it.
function _newerUndecided(row, rows) {
  const group = _supersedeGroup(row.action_type)
  if (!group) return false
  return rows.some((o) => o.id > row.id
    && (o.status === QUEUE_STATUS.PENDING
      || (o.status === QUEUE_STATUS.FAILED && o.uncertain && !o.acknowledged))
    && _supersedeGroup(o.action_type) === group
    && String(o.resource_id || '') === String(row.resource_id || ''))
}

async function _retireOlderRefusals(entry) {
  const group = _supersedeGroup(entry.action_type)
  if (!group) return
  try {
    const older = (await db.sync_queue.where('status').equals(QUEUE_STATUS.FAILED).toArray())
      .filter((r) => r.id < entry.id
        && _supersedeGroup(r.action_type) === group
        && String(r.resource_id || '') === String(entry.resource_id || ''))
    for (const r of older) await db.sync_queue.update(r.id, { acknowledged: true, superseded: true })
  } catch { /* best effort — the strip still shows it, which is the safe side */ }
}

// A row the page died on mid-send (app swiped away on one bar, iOS evicting the
// tab, the post-deploy chunk reload) stayed SYNCING forever: syncNow reads only
// PENDING, the counts read PENDING and FAILED — told it would submit, never did,
// nobody said so (#528's class). Put it back in line.
//
// Only rows attempted BEFORE THIS PAGE LOADED: those sends died with the old
// page. A row attempted in this page may still be in flight — a closeout with a
// signature image on one bar can take minutes — and resending it while the
// first request runs executes it twice (the idempotency cache is written only
// after a response). Residual: a second tab opened mid-send would still count
// the first tab's row as old.
function _pageStartedAt() {
  try {
    const t = performance?.timeOrigin
    if (Number.isFinite(t) && t > 0) return t
  } catch { /* no performance API */ }
  return Date.now()
}
const PAGE_STARTED_AT = _pageStartedAt()

//
// A send the phone got no clear answer to — the page died on it, the network
// dropped it, the server 5xx'd — may have run on the server anyway. A POST sent
// again is then answered from the server's cache of that first answer, under
// the row's Idempotency-Key, only while the cache holds it (24 h —
// core/middleware/idempotency_keys.py), less an hour for clock skew; past that
// it runs a second time. A partial cash payment or a part used whose answer was
// lost on Friday and replayed on Monday is recorded twice. The cache is
// POST-only: the queued PATCHes (address fix, customer contact, parts status)
// are never answered from it — a second copy SETS the same value again instead
// of adding a row, but days late it can set it over a newer edit.
//
// So the first such attempt is stamped (first_maybe_ran_at — the FIRST, since a
// copy that ran then is the one the cache holds), and a row past the window is
// never replayed on its own: it goes to the strip as "may have sent", and the
// tech or the office checks before anything is resent (see _claim).
const REPLAY_SAFE_MS = 23 * 3600_000

function _pastReplayWindow(since) {
  const t = Date.parse(since)
  return Number.isFinite(t) && !(Date.now() - t < REPLAY_SAFE_MS)
}

const UNCERTAIN = {
  status: QUEUE_STATUS.FAILED, uncertain: true, acknowledged: false,
  last_error: 'no_clear_answer', last_error_code: null, last_error_missing: null, last_error_reason: null,
}

async function _recoverStaleSyncing() {
  try {
    const stuck = (await db.sync_queue.where('status').equals(QUEUE_STATUS.SYNCING).toArray())
      .filter((r) => !r.last_attempted_at || Date.parse(r.last_attempted_at) < PAGE_STARTED_AT)
    for (const r of stuck) {
      const since = r.first_maybe_ran_at || r.last_attempted_at || r.created_at
      // Never a payment, at any age — a tech is never offered Retry on one, and
      // the queue must not do on its own what the tech can't. The dead page's
      // request may even still be running on the server as this page loads,
      // before any answer is cached.
      const replayable = isRetryable(r.action_type) && !_pastReplayWindow(since)
      await db.sync_queue.update(r.id, replayable
        ? { status: QUEUE_STATUS.PENDING, first_maybe_ran_at: since }
        : UNCERTAIN)
    }
  } catch { /* store unavailable */ }
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
    // Endpoints that use 409 for BUSINESS refusals (payments: void invoice,
    // closed-out deposit, locked GL period) opt in here so the refusal is
    // surfaced instead of being filed as "synced".
    //
    // Idempotency-Key coverage, stated honestly (M36, 2026-08-24): the
    // middleware DOES run now — PrincipalStampMiddleware feeds it — so a
    // SEQUENTIAL replay of a POST (drain retry after a lost response) is
    // served from cache — for 24 h; past that the claim hands the row to the
    // tech instead of replaying it (REPLAY_SAFE_MS, #528). What it does NOT
    // cover: two CONCURRENT drains of
    // the same row (SS-14 has no in-flight lock), and patchQueued replays
    // (the cache is POST-only — the header on PATCH is decoration). So
    // endpoints queued here still keep their own server-side dedupe as the
    // belt. Payments has one: an exact-reference match, plus a short window
    // for reference-less (cash) payments.
    conflict_is_error: !!opts.conflictIsError,
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
      // acknowledgeRefusal: a 4xx here is thrown to the caller, who shows it —
      // so the row is kept but never listed in failedActions (see below).
      const result = await _drainOne({ ...entry, id }, { acknowledgeRefusal: true })
      // Successful path returns parsed JSON or null.
      return result
    } catch (e) {
      // 4xx (validation/authz) is a real answer, not an outage: the row is
      // already marked FAILED and will never be replayed, so returning the
      // "queued" stub here would tell the caller a dead request was saved.
      // Rethrow so the caller's error path runs. (An unflagged 409 never
      // reaches here — _drainOne treats it as synced; a conflict_is_error
      // 409 throws like any other 4xx so the caller can show the refusal.)
      //
      // 401 is the exception: _drainOne left it PENDING to retry, so it is an
      // outage in disguise. Rethrowing would tell the tech "could not save"
      // about a write that is queued and will land.
      if (e?.status === 401 && e.transient) return { queued: true, idempotency_key }
      // A drain claimed the row first — it is being delivered. Or another write
      // of its kind for this job is still on the wire: it waits in line and is
      // sent the moment that one resolves (see _claim) — `waiting`, so the
      // caller doesn't tell a tech with full signal that they have none.
      if (e?.claimed) return { queued: true, idempotency_key, ...(e.busy ? { waiting: true } : {}) }
      // The caller is about to show this refusal (the closeout sheet stays open
      // on it), so it is not a SILENT failure: _drainOne marked the row
      // acknowledged — kept, but out of failedActions, or the strip would
      // report it a second time and go on saying "didn't send" after the tech
      // fixed it and resubmitted.
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

// Claim a row for sending: PENDING → SYNCING in ONE read-write transaction, or
// nothing. The immediate send in queueAction is not under the drain lock, and a
// drain's snapshot can hold a row the immediate send is about to post — both
// used to send it (the idempotency cache is written only after a response, so
// two concurrent copies both execute). IndexedDB serializes read-write
// transactions on a store, so exactly one claimer wins.
//
// It is also where "latest wins" is enforced as far as ONE phone can: it sees
// only its own writes, and learns a write landed only from an answer. (Settling
// it for every phone belongs on the server — a durable idempotency key and a
// compare-and-set on closeouts, #703.) Every send passes through here — the
// immediate send AND the drain, two senders with no order between them. The
// server keeps whichever write of a kind arrives LAST (a closeout received
// becomes the current one — its hours, its parts, its invoice), so for a row
// with a SUPERSEDE_GROUP, within the same resource:
//  - a NEWER write on the server (landed, or retired for one that did) retires
//    it, unsent;
//  - a NEWER write whose outcome is unknown — still PENDING, or "may have
//    sent" in the strip — goes first (newerFirst), and this one waits for it.
//    The phone cannot tell "never arrived" from "landed, but the answer was
//    lost on one bar": both leave the newer row unsettled. Sent first, the
//    older one would run and become current, and the newer one's replay would
//    then be answered from the Idempotency-Key cache WITHOUT running — the
//    server ends on the 9am closeout, the phone shows both sent. Newer first,
//    its answer says what happened: landed → this one retires; refused → this
//    one is sent (a redo refused on the spot replaced nothing); "may have
//    sent" → this one waits for the tech to check it;
//  - ANOTHER write still ON THE WIRE (SYNCING) makes it wait: arrival order
//    is not send order, so an older one sent beside a newer one still
//    uploading its signature can land second. The wait ends when the one on
//    the wire resolves (see _waitingBehind), and the claim decides again.
//
// A row that waits stays PENDING, untouched.
const _waitingBehind = new Set()

async function _claim(id) {
  return db.transaction('rw', db.sync_queue, async () => {
    const row = await db.sync_queue.get(id)
    if (!row || row.status !== QUEUE_STATUS.PENDING) return { claimed: false }
    // An earlier copy may have run, and the server no longer holds its answer:
    // sent now, it would run again (see REPLAY_SAFE_MS).
    if (row.first_maybe_ran_at && _pastReplayWindow(row.first_maybe_ran_at)) {
      await db.sync_queue.update(id, UNCERTAIN)
      return { claimed: false, uncertain: true }
    }
    const group = _supersedeGroup(row.action_type)
    if (group) {
      const sameResource = await db.sync_queue
        .where('resource_id').equals(row.resource_id || '')
        .toArray()
      if (_newerLanded(row, sameResource)) {
        await db.sync_queue.update(id, {
          status: QUEUE_STATUS.FAILED, acknowledged: true, superseded: true,
          last_error: 'replaced by a newer one on this phone',
        })
        return { claimed: false, superseded: true }
      }
      const onTheWire = sameResource.find((o) => o.id !== row.id
        && o.status === QUEUE_STATUS.SYNCING
        && _supersedeGroup(o.action_type) === group)
      if (onTheWire) {
        // Recorded INSIDE this transaction. The sender's own status write is a
        // later transaction on the same store, so it cannot slip between this
        // read and the note — when it finishes, it sees the note and runs a drain.
        _waitingBehind.add(onTheWire.id)
        return { claimed: false, busy: true }
      }
      if (_newerUndecided(row, sameResource)) return { claimed: false, newerFirst: true }
    }
    const last_attempted_at = new Date().toISOString()
    await db.sync_queue.update(id, { status: QUEUE_STATUS.SYNCING, last_attempted_at })
    return { claimed: true, row: { ...row, status: QUEUE_STATUS.SYNCING, last_attempted_at } }
  })
}

async function _drainOne(snapshot, opts = {}) {
  const claim = await _claim(snapshot.id)
  if (!claim.claimed) {
    const e = new Error(claim.superseded ? 'superseded' : claim.busy || claim.newerFirst ? 'waiting' : 'already_claimed')
    e.claimed = true
    e.superseded = !!claim.superseded
    e.busy = !!claim.busy
    e.newerFirst = !!claim.newerFirst
    throw e
  }
  try {
    return await _send(claim.row, opts)
  } finally {
    // A write of this row's group waited for this one to leave the wire; its
    // status is written by now (every branch of _send awaits it), so a drain
    // started here decides it on the outcome. Not awaited: the immediate send
    // returns to its caller; inside a drain this only asks for one more pass.
    if (_waitingBehind.delete(claim.row.id)) Promise.resolve().then(() => syncNow())
  }
}

async function _send(entry, { acknowledgeRefusal = false } = {}) {
  let token = null
  try {
    // Read auth at the moment of replay — token may have refreshed since
    // the action was queued.
    token = sessionStorage.getItem('gdx_access_token') || null
  } catch {}
  const headers = {
    'Content-Type': 'application/json',
    // Server middleware is Stripe-shaped; bare `Idempotency-Key` is the
    // canonical header (see gdx/core/middleware/idempotency.py).
    'Idempotency-Key': entry.idempotency_key,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
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
    // Network down — flip back to pending, increment attempts. The request may
    // have reached the server and run before the answer was lost: stamp it
    // (see REPLAY_SAFE_MS).
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.PENDING,
      attempt_count: (entry.attempt_count || 0) + 1,
      last_error: netErr.message || 'network_error',
      first_maybe_ran_at: entry.first_maybe_ran_at || entry.last_attempted_at,
    })
    await _refreshPendingCount()
    throw netErr
  }

  // 2xx OR 409 (assumed server-side dedup of a duplicate replay) → synced.
  // EXCEPT entries flagged conflict_is_error: their endpoints 409 business
  // refusals (e.g. payments against a void invoice or locked GL period) —
  // filing those as synced showed "Payment recorded" for money the server
  // refused. Flagged 409s fall through to the 4xx-failed branch below.
  if (resp.ok || (resp.status === 409 && !entry.conflict_is_error)) {
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.SYNCED,
      last_error: null,
      last_error_code: resp.status,
    })
    await _retireOlderRefusals(entry)
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
      detail = _detailText(parsedBody, resp.status)
    } catch {}
    await db.sync_queue.update(entry.id, {
      status: QUEUE_STATUS.FAILED,
      last_error: detail,
      last_error_code: resp.status,
      // The closeout gate's checklist ("add: signature, hours") — the one
      // refusal detail a tech can act on without calling the office.
      last_error_missing: Array.isArray(parsedBody?.missing) ? parsedBody.missing : null,
      last_error_reason: typeof parsedBody?.detail?.code === 'string' ? parsedBody.detail.code : null,
      acknowledged: acknowledgeRefusal,
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

  // 5xx / 502 / 503 — transient; retry on next drain. Not a verdict either: a
  // 504 is the proxy giving up on a handler that may still commit, a 500 can
  // come after one did — stamped like a lost answer.
  await db.sync_queue.update(entry.id, {
    status: QUEUE_STATUS.PENDING,
    attempt_count: (entry.attempt_count || 0) + 1,
    last_error: `HTTP ${resp.status}`,
    last_error_code: resp.status,
    first_maybe_ran_at: entry.first_maybe_ran_at || entry.last_attempted_at,
  })
  await _refreshPendingCount()
  const e = new Error(`HTTP ${resp.status}`)
  e.status = resp.status
  throw e
}

/**
 * Drain all pending entries (FIFO). Stops on a network error, a stale token or
 * a server that is down, so we don't churn the queue. One drain at a time: a
 * call while a drain is in flight asks for one more pass after it, instead of
 * being dropped.
 */
// The server is down, not choking on one request: stop, don't walk the queue.
const SERVER_DOWN = new Set([502, 503, 504])

// A syncNow() that arrives while a drain runs is not dropped: the running
// drain works from a snapshot taken when it started, so a row put back in line
// after that (a Retry) would otherwise sit until the next online/visibility
// event — "waiting for signal" with full signal.
let _rerunRequested = false

export async function syncNow() {
  if (syncing.value) { _rerunRequested = true; return }
  if (!isOnline.value) return
  // Claimed BEFORE any await: two calls in the same tick (a view and a reader
  // mounting together) must not both drain the same rows.
  syncing.value = true
  try {
    await _recoverStaleSyncing()
    const pending = await db.sync_queue
      .where('status').equals(QUEUE_STATUS.PENDING)
      .sortBy('created_at')
    // This drain sends one row at a time, oldest first, and a row the server
    // 5xx'd — or one waiting for a write of its kind still on the wire — holds
    // back every NEWER row for the same job this pass: the server orders a
    // job's writes too (dispatch status only moves forward), so an arrival
    // that 500'd must not be overtaken by that job's closeout. The one
    // exception is inside a "latest wins" group, where the NEWEST write goes
    // first and the older ones stand aside until it is decided (see _claim).
    // That order holds within this drain only; the immediate send in
    // queueAction is a second sender, and _claim keeps the two from racing.
    const heldBack = new Set()
    const stoodAside = []
    // false → stop the drain.
    const attempt = async (entry, { standAside = null } = {}) => {
      const key = entry.resource_id ? `r:${entry.resource_id}` : `a:${entry.action_type}`
      if (heldBack.has(key)) return true
      try {
        await _drainOne(entry)
      } catch (e) {
        if (e?.claimed) {
          // Waiting on a write still on the wire: nothing newer for this job
          // goes ahead of it. Standing aside for a newer one of its kind:
          // decided after this pass. (Lost the claim or retired: nothing to do.)
          if (e.busy) heldBack.add(key)
          else if (e.newerFirst && standAside) standAside.push(entry)
          return true
        }
        // Network down, the server down, or the token stale (every request
        // after it would 401 too, and a token refreshed mid-pass would let
        // newer rows land before this one) — bail; the queue stays for later.
        if (!e?.status || SERVER_DOWN.has(e.status) || e.status === 401) return false
        // Another 5xx is THIS request's trouble: stopping here let one row the
        // server chokes on hide every refusal queued behind it. Hold its job
        // back and carry on. (4xx — already marked failed.)
        if (e.status >= 500) heldBack.add(key)
      }
      return true
    }
    let carryOn = true
    for (const entry of pending) {
      carryOn = await attempt(entry, { standAside: stoodAside })
      if (!carryOn) break
    }
    // Every newer write they stood aside for has been tried now. Newest first:
    // the claim retires each whose newer one landed and sends it if that one
    // was refused; a newer one still PENDING (5xx'd, so its job is held back)
    // keeps it waiting for the next drain.
    if (carryOn) {
      for (const entry of stoodAside.reverse()) {
        if (!(await attempt(entry))) break
      }
    }
    const stamp = new Date().toISOString()
    await setMetadata('last_synced_at', stamp)
    lastSyncedAt.value = stamp
  } finally {
    syncing.value = false
    await _refreshPendingCount()
    if (_rerunRequested) {
      _rerunRequested = false
      await syncNow()
    }
  }
}

// Until no drain is running and none is queued up behind it.
async function _waitForDrainIdle(maxMs = 60_000) {
  for (let t = 0; t < maxMs && (syncing.value || _rerunRequested); t += 100) {
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}

function _matches(row, { jobId = null, ids = null } = {}) {
  if (row.acknowledged) return false
  if (ids && !ids.includes(row.id)) return false
  if (jobId && String(row.resource_id) !== String(jobId)) return false
  return true
}

// A payment the server refused is money it said no to — a duplicate, a void
// invoice, nothing left to pay. Replaying it later, after the office recorded it
// by hand, records it twice. The strip offers Discard only; the office decides.
const NOT_RETRYABLE = new Set(['invoice.payment'])

export function isRetryable(actionType) {
  return !NOT_RETRYABLE.has(actionType)
}

async function _authedGet(url) {
  let token = null
  try { token = sessionStorage.getItem('gdx_access_token') || null } catch {}
  const resp = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: 'include',
  })
  if (!resp.ok) return null
  try { return await resp.json() } catch { return null }
}

// Before resending a refused closeout: has this job been closed out SINCE?
// (Closed out again by the tech, or by someone else.) Resending then would
// supersede the newer closeout with the stale one. Unknown (offline, job
// missing) resends — the server will refuse again and say why.
async function _closeoutIsStale(row) {
  try {
    const r = await _authedGet(`/api/mobile/job/${encodeURIComponent(row.resource_id)}`)
    const closedAt = r?.job?.closeout?.closed_at
    return Boolean(closedAt && Date.parse(closedAt) > Date.parse(row.created_at))
  } catch {
    return false
  }
}

/**
 * Put refused writes back in line and drain — ONLY the rows passed (the ones
 * the tech was shown when they tapped). For what a retry can fix: the job was
 * restored or reassigned back, the office created the invoice the closeout
 * gate wanted, a permission was restored. A payment is never replayed; a
 * closeout the job has since moved past is retired instead of resent.
 */
export async function retryFailedActions({ jobId = null, ids = null } = {}) {
  const outcome = {
    retried: 0, sent: 0, refused: 0, pending: 0, superseded: 0, supersededBy: null, skipped: 0,
    first: null, online: isOnline.value,
  }
  let rows = []
  try {
    rows = (await db.sync_queue.where('status').equals(QUEUE_STATUS.FAILED).sortBy('created_at'))
      .filter((r) => _matches(r, { jobId, ids }))
  } catch { /* store unavailable — nothing to retry */ }
  const resend = []
  let all = []
  try { all = await db.sync_queue.toArray() } catch { /* store unavailable */ }
  for (const r of rows) {
    if (!isRetryable(r.action_type)) { outcome.skipped += 1; continue }
    // A newer write of its group has already LANDED: this one is obsolete, and
    // sending it would put stale hours, parts and signature over it. (A newer
    // one that was refused replaced nothing — this one stays sendable.)
    if (_newerLanded(r, all)) {
      try { await db.sync_queue.update(r.id, { acknowledged: true, superseded: true }) } catch {}
      outcome.superseded += 1
      outcome.supersededBy = 'newer_on_phone'
      continue
    }
    // "Closed out since" is read off the server's clock, which cannot say WHO
    // closed it out. If this phone's own OLDER closeout for the job landed
    // after this one was made (this one was refused, so that one went out), the
    // server's closeout may be that one — and resending this newer one is
    // exactly right. Only a closeout this phone did not send retires it.
    const ownOlderLandedSince = r.action_type === 'job.closeout' && all.some((o) => o.id < r.id
      && o.status === QUEUE_STATUS.SYNCED
      && o.action_type === 'job.closeout'
      && String(o.resource_id || '') === String(r.resource_id || '')
      && Date.parse(o.last_attempted_at) >= Date.parse(r.created_at))
    if (r.action_type === 'job.closeout' && !ownOlderLandedSince && await _closeoutIsStale(r)) {
      try { await db.sync_queue.update(r.id, { acknowledged: true, superseded: true }) } catch {}
      outcome.superseded += 1
      outcome.supersededBy = outcome.supersededBy || 'closed_since'
      continue
    }
    try {
      await db.sync_queue.update(r.id, {
        status: QUEUE_STATUS.PENDING, last_error: null, last_error_code: null, last_error_missing: null,
        // The tech looked and chose to send it again: that is the check a row
        // past the replay window was waiting for.
        last_error_reason: null, uncertain: false, first_maybe_ran_at: null,
      })
      resend.push(r)
    } catch { /* leave it failed */ }
  }
  outcome.retried = resend.length
  await _refreshPendingCount()
  // A drain may already be walking the queue (coming back from a confirm
  // dialog fires visibilitychange). syncNow() then asks for a rerun after it
  // instead of returning empty-handed; wait for that before reporting.
  if (resend.length) {
    await syncNow()
    await _waitForDrainIdle()
  }
  for (const r of resend) {
    let after = null
    try { after = await db.sync_queue.get(r.id) } catch { /* treat as gone */ }
    if (!after || after.status === QUEUE_STATUS.SYNCED) outcome.sent += 1
    else if (after.status === QUEUE_STATUS.FAILED) {
      outcome.refused += 1
      if (!outcome.first) {
        outcome.first = {
          action_type: after.action_type, http_status: after.last_error_code ?? null,
          error: after.last_error || null, missing: after.last_error_missing || [],
        }
      }
    } else {
      outcome.pending += 1
      if ((after.last_error_code ?? 0) >= 500) outcome.serverError = true
    }
  }
  outcome.online = isOnline.value
  await _refreshPendingCount()
  return outcome
}

/**
 * Delete refused writes from this phone — ONLY the rows passed. Destructive:
 * for a closeout the row holds the only copy of the parts, hours and
 * signature, so callers confirm with the tech first, naming what goes. Never
 * called by the queue itself.
 */
export async function discardFailedActions({ jobId = null, ids = null } = {}) {
  let rows = []
  try {
    rows = (await db.sync_queue.where('status').equals(QUEUE_STATUS.FAILED).sortBy('created_at'))
      .filter((r) => _matches(r, { jobId, ids }))
    for (const r of rows) await db.sync_queue.delete(r.id)
  } catch { /* store unavailable — nothing to discard */ }
  await _refreshPendingCount()
  return rows.length
}

const ACTION_LABELS = {
  'job.closeout': 'Closeout',
  'job.note': 'Note',
  'job.part_needed': 'Part request',
  'job.part_used': 'Part used',
  'job.arrived': 'Arrival',
  'job.en_route': 'On-my-way',
  'job.site_fix': 'Address fix',
  'job.customer_contact': 'Customer contact',
  'job.chat': 'Chat message',
  'today.reorder': "Today's job order",
  'parts.status': 'Parts status',
  'change_order.create': 'Change order',
  'invoice.payment': 'Payment',
}

/** What a queued write was, in the words a tech uses. */
export function describeQueuedAction(actionType) {
  return ACTION_LABELS[actionType] || 'A change'
}

const MISSING_LABELS = {
  parts: 'parts logged',
  hours: 'labor hours',
  signature: 'customer signature',
  invoice: 'an invoice',
  return_visit_reason: 'why the return visit is needed',
}

// What a 404 lost, by what the row was written against (its resource_id): a
// payment names an invoice, a parts status a part request, Today's order a
// list of jobs — everything else a job.
const GONE_LABELS = {
  'invoice.payment': 'That invoice',
  'parts.status': 'That part request',
  'today.reorder': 'One of those jobs',
}

/** Why the server refused it — what the tech can actually do about it. */
export function describeQueuedRefusal({ action_type: actionType, http_status: status, error, missing, uncertain } = {}) {
  if (uncertain) {
    return 'The phone never got a clear answer while sending it — it may already be on the server.'
      + (isRetryable(actionType) ? ' Check the job before you Retry.' : '')
  }
  if (Array.isArray(missing) && missing.length) {
    return 'Still needs: ' + missing.map((m) => MISSING_LABELS[m] || m).join(', ')
  }
  switch (status) {
    case 403: return "You're not allowed to do that any more."
    case 404: return `${GONE_LABELS[actionType] || 'That job'} no longer exists on the server.`
    default:
      return error && !/^HTTP \d+$/.test(error) ? `The server said: ${error}` : 'The server refused it.'
  }
}

/**
 * Purge entries that have been in 'synced' status for longer than the
 * given window (default 7 days). Called opportunistically — the queue
 * stays clean over time but doesn't churn on every call.
 */
export async function purgeOldSynced(maxAgeDays = 7) {
  const cutoff = new Date(Date.now() - maxAgeDays * 86400_000).toISOString()
  const all = await db.sync_queue.toArray()
  // A landed row — or one retired for a newer one that landed — is the
  // evidence the claim uses to retire an older unsent one of its group: keep it
  // while such a row is still on the phone.
  const unsent = all.filter((r) => r.status === QUEUE_STATUS.PENDING
    || (r.status === QUEUE_STATUS.FAILED && !r.superseded))
  const needed = (evidence) => {
    const group = _supersedeGroup(evidence.action_type)
    return group && unsent.some((u) => u.id < evidence.id
      && _supersedeGroup(u.action_type) === group
      && String(u.resource_id || '') === String(evidence.resource_id || ''))
  }
  await db.sync_queue
    .where('status').equals(QUEUE_STATUS.SYNCED)
    .filter(e => e.last_attempted_at && e.last_attempted_at < cutoff && !needed(e))
    .delete()
  // Refusals the tech already saw, or that a newer write replaced (#528): they
  // never leave the strip's reach otherwise — Discard skips them, and every
  // refresh reads the whole FAILED set, bodies and signatures included. An
  // UNSEEN refusal is never purged: it is the strip's to show until the tech acts.
  await db.sync_queue
    .where('status').equals(QUEUE_STATUS.FAILED)
    .filter(e => e.acknowledged && (e.last_attempted_at || e.created_at) < cutoff
      && !(e.superseded && needed(e)))
    .delete()
}

let _purgedThisPage = false

/**
 * Composable wrapper — installs window/visibility listeners that auto-
 * sync, exposes the reactive state. Safe to call from many components;
 * the underlying store is a singleton.
 */
export function useOfflineSync() {
  let onlineHandler
  let visibilityHandler
  // The mount hook awaits two IndexedDB reads before it installs listeners,
  // and the continuation used to run whatever had happened meanwhile:
  //  - in the app, a route change unmounted the host during the awaits and
  //    the listeners were re-added AFTER onUnmounted had removed them — the
  //    leak `disposed` closes;
  //  - in vitest, nothing unmounts these hosts: the jsdom environment is
  //    torn down after the file and its globals are DELETED, so the
  //    continuation threw "ReferenceError: window is not defined" as an
  //    unhandled rejection (2026-08-31) — the `typeof window` check below
  //    is what closes that one.
  let disposed = false

  onMounted(async () => {
    await _refreshPendingCount() // also loads failedActions from the phone
    await _hydrateLastSyncedAt()
    if (disposed || typeof window === 'undefined' || typeof document === 'undefined') return
    onlineHandler = () => { syncNow() }
    visibilityHandler = () => {
      if (!document.hidden && isOnline.value) syncNow()
    }
    window.addEventListener('online', onlineHandler)
    document.addEventListener('visibilitychange', visibilityHandler)
    // If we landed online with a pending queue (e.g. tab restore), drain.
    if (isOnline.value) {
      // Defer a tick so other onMounted hooks finish first.
      Promise.resolve().then(() => { if (!disposed) syncNow() })
    }
    // Once per page load — it was exported and never called, so the queue only
    // ever grew.
    if (!_purgedThisPage) {
      _purgedThisPage = true
      purgeOldSynced().then(_refreshFailedActions).catch(() => { /* best effort */ })
    }
  })

  onUnmounted(() => {
    disposed = true
    if (onlineHandler) window.removeEventListener('online', onlineHandler)
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler)
  })

  return {
    isOnline,
    pendingCount,
    syncing,
    lastSyncedAt,
    failedActions,
    queueAction,
    syncNow,
    purgeOldSynced,
    retryFailedActions,
    discardFailedActions,
    describeQueuedAction,
    describeQueuedRefusal,
    isRetryable,
  }
}
