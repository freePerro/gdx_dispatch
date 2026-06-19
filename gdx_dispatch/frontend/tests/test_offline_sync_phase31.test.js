/**
 * Sprint tech_mobile Phase 3 (S3-A2 + S3-A3) — action queue + sync engine.
 *
 * Validates:
 *   - queueAction inserts a pending row + sets idempotency_key
 *   - successful drain marks the row as synced
 *   - 4xx response marks the row as failed (no retry)
 *   - 5xx response leaves the row pending (will retry)
 *   - 409 (server-side dedup) is treated as success-by-replay
 *   - syncNow drains in FIFO order
 */
import 'fake-indexeddb/auto'
import { describe, it, expect, beforeEach, vi } from 'vitest'

// Reset Dexie module + queue state per test by re-importing.
async function freshSync() {
  vi.resetModules()
  // Wipe IndexedDB
  if (typeof indexedDB !== 'undefined' && indexedDB.databases) {
    const dbs = await indexedDB.databases()
    for (const d of dbs) {
      if (d.name) await new Promise((r) => {
        const req = indexedDB.deleteDatabase(d.name)
        req.onsuccess = req.onerror = req.onblocked = () => r()
      })
    }
  }
  const mod = await import('../src/composables/useOfflineSync.js')
  const dbMod = await import('../src/lib/offlineDb.js')
  return { ...mod, ...dbMod }
}

describe('Phase 3 offline action queue', () => {
  beforeEach(() => {
    // sessionStorage stub (vitest node env — no jsdom)
    const store = {}
    globalThis.sessionStorage = {
      getItem: (k) => store[k] || null,
      setItem: (k, v) => { store[k] = String(v) },
      removeItem: (k) => { delete store[k] },
    }
    if (typeof globalThis.window === 'undefined') {
      globalThis.window = { location: { hostname: 'gdx.example.com' } }
    }
    // navigator.onLine — Phase 3 sync engine reads this to decide whether
    // to attempt the request immediately. Force online in tests; we
    // explicitly model offline by having fetch reject (network unreachable).
    try {
      Object.defineProperty(globalThis, 'navigator', {
        value: { onLine: true }, configurable: true, writable: true,
      })
    } catch {
      // navigator already defined; mutate the property if possible.
      try { globalThis.navigator.onLine = true } catch {}
    }
  })

  it('queues a mutation and marks it synced on 201', async () => {
    const { queueAction, db, QUEUE_STATUS } = await freshSync()
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 201,
      json: async () => ({ id: 'job-1', status: 'en_route' }),
    })
    const result = await queueAction('POST', '/api/mobile/jobs/job-1/en-route', { eta_minutes: 5 })
    expect(result).toEqual({ id: 'job-1', status: 'en_route' })
    const rows = await db.sync_queue.toArray()
    expect(rows).toHaveLength(1)
    expect(rows[0].status).toBe(QUEUE_STATUS.SYNCED)
    expect(rows[0].idempotency_key).toMatch(/^[0-9a-f-]{36}$/)
    // Idempotency-Key header was sent
    const callHeaders = globalThis.fetch.mock.calls[0][1].headers
    expect(callHeaders['Idempotency-Key']).toBe(rows[0].idempotency_key)
  })

  it('treats 409 as success-by-replay', async () => {
    const { queueAction, db, QUEUE_STATUS } = await freshSync()
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false, status: 409,
      json: async () => ({ replay: true }),
    })
    await queueAction('POST', '/api/mobile/jobs/job-1/complete', { signature_data: 'x' })
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.SYNCED)
    expect(rows[0].last_error_code).toBe(409)
  })

  it('marks 4xx (non-409) as failed without retry', async () => {
    const { queueAction, db, QUEUE_STATUS } = await freshSync()
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false, status: 400,
      json: async () => ({ detail: 'bad payload' }),
    })
    await queueAction('POST', '/api/mobile/jobs/job-1/en-route', {})
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.FAILED)
    expect(rows[0].last_error).toBe('bad payload')
  })

  it('keeps row pending on 503; syncNow retries successfully', async () => {
    const { queueAction, syncNow, db, QUEUE_STATUS } = await freshSync()
    globalThis.fetch = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ ok: true }) })
    await queueAction('POST', '/api/mobile/jobs/job-1/arrived', {})
    let rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.PENDING)
    expect(rows[0].attempt_count).toBe(1)
    await syncNow()
    rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.SYNCED)
  })

  it('drains FIFO via syncNow', async () => {
    const { queueAction, syncNow, db, QUEUE_STATUS } = await freshSync()
    // Both inserts happen offline-style (network unreachable on insert).
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('offline'))
    await queueAction('POST', '/api/a', {})
    await queueAction('POST', '/api/b', {})
    let rows = await db.sync_queue.orderBy('created_at').toArray()
    expect(rows.map(r => r.url)).toEqual(['/api/a', '/api/b'])
    expect(rows.every(r => r.status === QUEUE_STATUS.PENDING)).toBe(true)

    // Network back; drain succeeds.
    const calls = []
    globalThis.fetch = vi.fn().mockImplementation(async (url) => {
      calls.push(url)
      return { ok: true, status: 200, json: async () => ({ url }) }
    })
    await syncNow()
    expect(calls).toEqual(['/api/a', '/api/b'])
    rows = await db.sync_queue.toArray()
    expect(rows.every(r => r.status === QUEUE_STATUS.SYNCED)).toBe(true)
  })
})
