/**
 * Sprint tech_mobile Phase 3 — Dexie schema for offline mode.
 *
 * Object stores per the design doc (`_research/sprint3_offline_design.md`):
 *
 *   sync_queue   — mutation outbox (status: pending|syncing|synced|failed)
 *   jobs         — denormalised job rows pulled from /api/mobile/today
 *   parts_needed — per-job parts requests
 *   photos       — captured photo blobs awaiting upload
 *   sync_metadata — k/v metadata (last_sync_at, schema_version, …)
 *
 * Conflict resolution v1: row-level last-write-wins via `synced_at`.
 * Photos use IndexedDB blob storage (no base64 overhead). All stores
 * are tenant-agnostic — the JWT in the queued payload determines the
 * tenant at replay time.
 */
import Dexie from 'dexie'

export const db = new Dexie('gdx_offline')

db.version(2).stores({
  // sync_queue: ++id PK; created_at index for FIFO replay; status index for
  // fast filtering. idempotency_key is a per-row uuid sent as
  // X-Idempotency-Key so a server-side dedup table can drop replays.
  sync_queue: '++id, status, created_at, action_type, resource_id, idempotency_key',

  // jobs: id PK matches /api/mobile/today's payload; dispatch_status +
  // synced_at indexes used by the offline-aware list filter.
  jobs: 'id, dispatch_status, synced_at, updated_at',

  // parts_needed: separate store for fast per-job lookups + sync state.
  parts_needed: 'id, job_id, status, synced_at',

  // photos: id PK + job_id index; blob stored as Blob (not base64).
  photos: 'id, job_id, status, created_at',

  // sync_metadata: simple k/v.
  sync_metadata: 'key',
})

// Bump to v2 cleanly when migrating from the v1 stub schema (which used
// a single sync_queue store with autoIncrement and no indexes). Dexie's
// versioning handles the migration automatically — old rows are kept.

// v3 (#528): same stores; a one-time pass over refusals already on the phone.
// Refused rows now surface in a "didn't send" strip unless `acknowledged`. Before
// v3, a refusal on the IMMEDIATE online attempt was thrown to the caller — who
// showed it ("Cannot close out yet", and the tech fixed it and resubmitted) —
// but was still left FAILED with no flag, so the strip would resurrect every one
// on the first day. The immediate attempt starts within milliseconds of the row
// being written; a background replay waits for signal to come back — which can
// be only seconds in a short dead zone, so the window is kept tight. Rows
// attempted within 2 s of creation are marked seen; the rest are genuine unseen
// refusals and stay.
const LEGACY_IMMEDIATE_MS = 2_000

db.version(3).stores({
  sync_queue: '++id, status, created_at, action_type, resource_id, idempotency_key',
  jobs: 'id, dispatch_status, synced_at, updated_at',
  parts_needed: 'id, job_id, status, synced_at',
  photos: 'id, job_id, status, created_at',
  sync_metadata: 'key',
}).upgrade((tx) =>
  tx.table('sync_queue').toCollection().modify((row) => {
    if (row.status !== 'failed' || 'acknowledged' in row) return
    const created = Date.parse(row.created_at)
    const attempted = Date.parse(row.last_attempted_at)
    if (Number.isFinite(created) && Number.isFinite(attempted) && attempted - created < LEGACY_IMMEDIATE_MS) {
      row.acknowledged = true
    }
  }),
)

export const QUEUE_STATUS = Object.freeze({
  PENDING: 'pending',
  SYNCING: 'syncing',
  SYNCED: 'synced',
  FAILED: 'failed',
})

export async function getMetadata(key, fallback = null) {
  const row = await db.sync_metadata.get(key)
  return row?.value ?? fallback
}

export async function setMetadata(key, value) {
  await db.sync_metadata.put({ key, value })
}
