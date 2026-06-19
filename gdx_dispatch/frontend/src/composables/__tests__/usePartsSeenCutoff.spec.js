import { describe, expect, it, beforeEach } from 'vitest'
import {
  SEEN_KEY,
  countUnseenForJob,
  markJobSeen,
  readSeen,
  writeSeen,
} from '../usePartsSeenCutoff'

function memStorage() {
  const m = new Map()
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  }
}

describe('usePartsSeenCutoff', () => {
  let store
  beforeEach(() => {
    store = memStorage()
  })

  it('readSeen returns {} when nothing stored', () => {
    expect(readSeen(store)).toEqual({})
  })

  it('readSeen tolerates corrupted JSON', () => {
    store.setItem(SEEN_KEY, 'not-json{')
    expect(readSeen(store)).toEqual({})
  })

  it('writeSeen + readSeen round-trip', () => {
    writeSeen({ 'job-a': '2026-05-02T12:00:00Z' }, store)
    expect(readSeen(store)).toEqual({ 'job-a': '2026-05-02T12:00:00Z' })
  })

  it('markJobSeen stamps current time onto the given job', () => {
    const t = new Date('2026-05-02T15:00:00Z')
    markJobSeen('job-a', store, t)
    expect(readSeen(store)['job-a']).toBe(t.toISOString())
  })

  it('countUnseenForJob returns 0 when the job has never been viewed', () => {
    const list = [{ id: 'p1', updated_at: '2026-05-02T12:00:00Z', status: 'ordered' }]
    expect(countUnseenForJob('job-a', list, store)).toBe(0)
  })

  it('counts only ordered/received updates after the cutoff', () => {
    markJobSeen('job-a', store, new Date('2026-05-02T10:00:00Z'))
    const list = [
      // After cutoff, ordered → counts
      { id: 'p1', updated_at: '2026-05-02T11:00:00Z', status: 'ordered' },
      // After cutoff, received → counts
      { id: 'p2', updated_at: '2026-05-02T12:00:00Z', status: 'received' },
      // After cutoff but still 'needed' → does NOT count (tech filed it)
      { id: 'p3', updated_at: '2026-05-02T13:00:00Z', status: 'needed' },
      // Before cutoff → does NOT count
      { id: 'p4', updated_at: '2026-05-02T09:00:00Z', status: 'ordered' },
      // No updated_at → ignored
      { id: 'p5', status: 'ordered' },
    ]
    expect(countUnseenForJob('job-a', list, store)).toBe(2)
  })

  it('countUnseenForJob handles empty / null list', () => {
    markJobSeen('job-a', store, new Date('2026-05-02T10:00:00Z'))
    expect(countUnseenForJob('job-a', [], store)).toBe(0)
    expect(countUnseenForJob('job-a', null, store)).toBe(0)
  })

  it('markJobSeen for one job does not affect another job\'s counter', () => {
    markJobSeen('job-a', store, new Date('2026-05-02T10:00:00Z'))
    const list = [{ id: 'p1', updated_at: '2026-05-02T11:00:00Z', status: 'ordered' }]
    expect(countUnseenForJob('job-a', list, store)).toBe(1)
    expect(countUnseenForJob('job-b', list, store)).toBe(0)
  })
})
