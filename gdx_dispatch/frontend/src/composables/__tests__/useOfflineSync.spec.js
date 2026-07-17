/**
 * 2026-07-01 UX audit — offline queue behavior. The queue infrastructure
 * predates these tests; they were added when the mobile mutations were
 * actually wired to it (en-route/arrived/reorder/closeout/chat/parts).
 *
 * Covers the caller-facing contract of queueAction():
 *   - offline           → { queued: true } stub, row stays PENDING
 *   - online, network ✗ → { queued: true } stub, row stays PENDING
 *   - online, 2xx       → parsed JSON returned, row SYNCED
 *   - online, 4xx       → THROWS (status + body preserved), row FAILED —
 *                         the pre-audit behavior returned the queued stub
 *                         here, telling callers a dead request was saved
 *   - online, 401       → { queued: true } stub, row PENDING. NOT a verdict on
 *                         the work: this queue replays hours later, so a stale
 *                         token is expected. (2026-07-16 — until then a 401
 *                         marked the row FAILED forever and the write vanished
 *                         with no toast and no trace.)
 *   - online, 5xx       → { queued: true } stub, row PENDING (retryable)
 *   - syncNow()         → drains PENDING rows FIFO with Idempotency-Key
 */
import 'fake-indexeddb/auto'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { queueAction, syncNow } from '../useOfflineSync'
import { db, QUEUE_STATUS } from '../../lib/offlineDb'
import { useOnlineState } from '../useOnlineState'

const { isOnline } = useOnlineState()

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

beforeEach(async () => {
  await db.sync_queue.clear()
  await db.sync_metadata.clear()
  isOnline.value = true
  global.fetch = vi.fn()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('queueAction', () => {
  it('offline → returns queued stub and leaves the row pending', async () => {
    isOnline.value = false
    const r = await queueAction('POST', '/api/jobs/1/closeout', { hours: 2 })
    expect(r.queued).toBe(true)
    expect(r.idempotency_key).toBeTruthy()
    expect(global.fetch).not.toHaveBeenCalled()
    const rows = await db.sync_queue.toArray()
    expect(rows).toHaveLength(1)
    expect(rows[0].status).toBe(QUEUE_STATUS.PENDING)
    expect(rows[0].body).toEqual({ hours: 2 })
  })

  it('online + network failure → queued stub, row stays pending for retry', async () => {
    global.fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const r = await queueAction('POST', '/api/mobile/jobs/9/en-route', {})
    expect(r.queued).toBe(true)
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.PENDING)
    expect(rows[0].attempt_count).toBe(1)
  })

  it('online + 401 → queued stub, row stays PENDING (expired token, not a verdict)', async () => {
    // The whole point of this queue is that writes sit for hours before they
    // replay, so meeting an expired token on reconnect is the NORMAL case, not
    // an error. Treating 401 like any other 4xx marked the row FAILED forever:
    // a tech's arrival — or the note they wrote in a dead zone — was one token
    // expiry away from silently disappearing. This replay path uses a bare
    // fetch and cannot refresh the token itself (useApi.request can, and
    // usePhotoQueue routes through it for exactly this reason), so the row must
    // survive for the next drain.
    global.fetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }))

    const r = await queueAction('POST', '/api/mobile/jobs/9/arrived', { lat: 1 })

    expect(r.queued).toBe(true)
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.PENDING)
    expect(rows[0].attempt_count).toBe(1)
  })

  it('online + 401 then 2xx on the next drain → the write lands', async () => {
    global.fetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }))
    await queueAction('POST', '/api/mobile/jobs/9/notes', { note: 'spring is 2in' })

    // The app refreshed its token in the meantime; the queue drains again.
    global.fetch.mockResolvedValueOnce(jsonResponse(201, { id: 'n1' }))
    await syncNow()

    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.SYNCED)
  })

  it('online + 2xx → returns the server JSON and marks the row synced', async () => {
    global.fetch.mockResolvedValueOnce(jsonResponse(200, { id: 42, ok: true }))
    const r = await queueAction('POST', '/api/mobile/jobs/9/arrived', { lat: 1 })
    expect(r).toEqual({ id: 42, ok: true })
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.SYNCED)
    // Idempotency-Key must ride along for server-side replay dedup.
    const headers = global.fetch.mock.calls[0][1].headers
    expect(headers['Idempotency-Key']).toBe(rows[0].idempotency_key)
  })

  it('online + 4xx → THROWS with status and parsed body; row failed (not "queued")', async () => {
    global.fetch.mockResolvedValueOnce(
      jsonResponse(422, { detail: 'missing gates', missing: ['signature'] })
    )
    let thrown = null
    try {
      await queueAction('POST', '/api/jobs/1/closeout', {})
    } catch (e) {
      thrown = e
    }
    expect(thrown).toBeTruthy()
    expect(thrown.status).toBe(422)
    // Closeout's "Cannot close out yet" UX reads err.body.missing.
    expect(thrown.body.missing).toEqual(['signature'])
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.FAILED)
  })

  it('online + 5xx → queued stub, row pending (transient, will retry)', async () => {
    global.fetch.mockResolvedValueOnce(jsonResponse(503, { detail: 'down' }))
    const r = await queueAction('POST', '/api/mobile/today/reorder', { appointment_ids: [1] })
    expect(r.queued).toBe(true)
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.PENDING)
  })

  it('online + 409 (server-side dedup) → treated as synced, not an error', async () => {
    global.fetch.mockResolvedValueOnce(jsonResponse(409, { detail: 'duplicate' }))
    const r = await queueAction('POST', '/api/jobs/1/closeout', {})
    expect(r).toEqual({ detail: 'duplicate' })
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.SYNCED)
  })
})

describe('syncNow', () => {
  it('drains pending rows FIFO and marks them synced', async () => {
    isOnline.value = false
    await queueAction('POST', '/api/a', { n: 1 })
    await queueAction('POST', '/api/b', { n: 2 })
    isOnline.value = true
    global.fetch.mockResolvedValue(jsonResponse(200, { ok: true }))
    await syncNow()
    expect(global.fetch).toHaveBeenCalledTimes(2)
    expect(global.fetch.mock.calls[0][0]).toBe('/api/a')
    expect(global.fetch.mock.calls[1][0]).toBe('/api/b')
    const statuses = (await db.sync_queue.toArray()).map((r) => r.status)
    expect(statuses).toEqual([QUEUE_STATUS.SYNCED, QUEUE_STATUS.SYNCED])
  })

  it('stops the drain on a network error and keeps the queue for later', async () => {
    isOnline.value = false
    await queueAction('POST', '/api/a', { n: 1 })
    await queueAction('POST', '/api/b', { n: 2 })
    isOnline.value = true
    global.fetch.mockRejectedValue(new TypeError('Failed to fetch'))
    await syncNow()
    // First entry attempted, drain bailed — second never fetched.
    expect(global.fetch).toHaveBeenCalledTimes(1)
    const statuses = (await db.sync_queue.toArray()).map((r) => r.status)
    expect(statuses).toEqual([QUEUE_STATUS.PENDING, QUEUE_STATUS.PENDING])
  })
})
