/**
 * offlineDb v3 (#528) — refusals already on a phone when the "didn't send"
 * strip ships. Before v3 a refusal on the IMMEDIATE online attempt was shown to
 * the tech by the caller but left FAILED with no flag; without this upgrade the
 * strip would resurrect every one of them on the first day. A genuine
 * background refusal (attempted long after it was queued) must stay visible.
 */
import 'fake-indexeddb/auto'
import Dexie from 'dexie'
import { describe, it, expect } from 'vitest'

const V2 = {
  sync_queue: '++id, status, created_at, action_type, resource_id, idempotency_key',
  jobs: 'id, dispatch_status, synced_at, updated_at',
  parts_needed: 'id, job_id, status, synced_at',
  photos: 'id, job_id, status, created_at',
  sync_metadata: 'key',
}

function row(key, createdMs, attemptedMs, status = 'failed') {
  return {
    idempotency_key: key, action_type: 'job.closeout', resource_id: 'job-1', method: 'POST',
    url: '/api/jobs/job-1/closeout', body: { hours: 1 }, headers: {}, status,
    last_error: 'completion requirements unmet', last_error_code: 422, attempt_count: 1,
    created_at: new Date(createdMs).toISOString(), last_attempted_at: new Date(attemptedMs).toISOString(),
  }
}

describe('offlineDb v2 → v3', () => {
  it('marks legacy immediate refusals seen and leaves background refusals visible', async () => {
    const t = Date.parse('2026-09-01T15:00:00Z')
    const legacy = new Dexie('gdx_offline')
    legacy.version(2).stores(V2)
    await legacy.open()
    await legacy.table('sync_queue').bulkAdd([
      row('immediate', t, t + 400), // refused on the spot — the tech saw the toast
      row('background', t, t + 3 * 3600_000), // refused hours later on reconnect — never seen
      row('short-dead-zone', t, t + 15_000), // 15 s without signal, refused on reconnect — never seen
      row('pending', t, t + 400, 'pending'),
    ])
    legacy.close()

    const { db } = await import('../offlineDb')
    const byKey = Object.fromEntries((await db.sync_queue.toArray()).map((r) => [r.idempotency_key, r]))
    expect(byKey.immediate.acknowledged).toBe(true)
    expect(byKey.background.acknowledged).toBeUndefined()
    expect(byKey['short-dead-zone'].acknowledged).toBeUndefined()
    expect(byKey.pending.acknowledged).toBeUndefined()
    expect(byKey.background.body).toEqual({ hours: 1 }) // nothing lost
  })
})
