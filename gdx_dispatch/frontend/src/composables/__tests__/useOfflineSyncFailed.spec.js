/**
 * #528 — a queued write the server refuses on a BACKGROUND replay is no longer
 * silent. The tech was told "saved offline — submits automatically when you
 * reconnect"; when the replay got a permanent 4xx the row went FAILED and no
 * code ever read FAILED rows. For a closeout that is a lost attestation.
 *
 * Also pinned (adversarial review of the first cut): a newer success retires
 * an older refusal of the same "latest wins" kind, so Retry can never resend a
 * stale closeout over a newer signed one; a payment is never replayed; Retry /
 * Discard touch only the rows passed; structured server errors read as words;
 * a row the page died on mid-send is recovered; one 5xx no longer hides the
 * refusals queued behind it. Sixth review: two writes of one kind for one job
 * are never on the wire together, and a dead send is replayed only while the
 * server's idempotency cache would catch a copy — never a payment.
 *
 * Real Dexie over fake-indexeddb, same harness as useOfflineSync.spec.js.
 */
import 'fake-indexeddb/auto'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  queueAction, syncNow, useOfflineSync,
  retryFailedActions, discardFailedActions, describeQueuedRefusal, describeQueuedAction, isRetryable,
} from '../useOfflineSync'
import { db, QUEUE_STATUS } from '../../lib/offlineDb'
import { useOnlineState } from '../useOnlineState'

const { isOnline } = useOnlineState()

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

// Route fetch by method + URL; each route is a queue of responses (last repeats).
let routes
function route(method, url, ...responses) {
  routes.push({ method, url, responses })
}
function installFetch() {
  global.fetch = vi.fn(async (url, init = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const r = routes.find((x) => x.method === method && (x.url === url || (x.url instanceof RegExp && x.url.test(url))))
    if (!r) throw new Error(`unrouted ${method} ${url}`)
    return r.responses.length > 1 ? r.responses.shift() : r.responses[0]
  })
}
function posts() {
  return global.fetch.mock.calls.filter(([, init]) => (init?.method || 'GET') !== 'GET')
}

function failed() {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  const s = useOfflineSync()
  warn.mockRestore()
  return s.failedActions.value
}

async function queueOffline(url, body, opts) {
  isOnline.value = false
  const r = await queueAction('POST', url, body, opts)
  isOnline.value = true
  return r
}

beforeEach(async () => {
  await db.sync_queue.clear()
  await db.sync_metadata.clear()
  isOnline.value = true
  routes = []
  installFetch()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('failedActions — what the tech is shown', () => {
  it('a closeout refused on a background replay is listed, with what it still needs', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { hours: 2 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout',
      jsonResponse(422, { detail: 'completion requirements unmet', missing: ['signature', 'hours'] }))
    await syncNow()

    const rows = failed()
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      action_type: 'job.closeout', resource_id: 'job-1', http_status: 422, missing: ['signature', 'hours'],
    })
    expect(describeQueuedRefusal(rows[0])).toBe('Still needs: customer signature, labor hours')
    expect((await db.sync_queue.get(rows[0].id)).body).toEqual({ hours: 2 }) // still on the phone
  })

  it('a refusal the caller already showed (online, immediate) is not reported twice', async () => {
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(422, { detail: 'nope', missing: ['signature'] }))
    await expect(
      queueAction('POST', '/api/jobs/job-1/closeout', { hours: 2 }, { actionType: 'job.closeout', resourceId: 'job-1' }),
    ).rejects.toMatchObject({ status: 422 })
    const rows = await db.sync_queue.toArray()
    expect(rows[0].status).toBe(QUEUE_STATUS.FAILED) // kept
    expect(failed()).toEqual([]) // but not listed
  })

  it('a closeout 409 on replay is a refusal, not "synced" (conflictIsError)', async () => {
    await queueOffline('/api/jobs/job-2/closeout', { hours: 1 }, {
      actionType: 'job.closeout', resourceId: 'job-2', conflictIsError: true,
    })
    route('POST', '/api/jobs/job-2/closeout', jsonResponse(409, { detail: 'Conflict' }))
    await syncNow()
    expect((await db.sync_queue.toArray())[0].status).toBe(QUEUE_STATUS.FAILED)
    expect(failed().map((r) => r.action_type)).toEqual(['job.closeout'])
  })

  it('structured server errors read as words, not "[object Object]"', async () => {
    await queueOffline('/api/invoices/inv-1/payments', { amount: 50 }, {
      actionType: 'invoice.payment', resourceId: 'inv-1', conflictIsError: true,
    })
    await queueOffline('/api/jobs/job-1/notes', { body: '' }, { actionType: 'job.note', resourceId: 'job-1' })
    route('POST', '/api/invoices/inv-1/payments',
      jsonResponse(409, { detail: { code: 'duplicate_payment', message: 'That payment is already recorded.' } }))
    route('POST', '/api/jobs/job-1/notes',
      jsonResponse(422, { detail: [{ loc: ['body', 'body'], msg: 'body must not be empty' }] }))
    await syncNow()
    const text = failed().map((r) => describeQueuedRefusal(r))
    expect(text).toEqual(['The server said: That payment is already recorded.', 'The server said: body must not be empty'])
  })
})

describe('a newer write of the same kind retires the older refusal', () => {
  it('closing the job out again retires the refused closeout — Retry can never resend it', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { hours: 2, signature_data: null }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout',
      jsonResponse(422, { detail: 'x', missing: ['signature'] }), jsonResponse(201, { ok: true }))
    await syncNow()
    expect(failed()).toHaveLength(1)

    // The tech closes out again, signed, online: it lands.
    await queueAction('POST', '/api/jobs/job-1/closeout', { hours: 2, signature_data: 'data:image/png;base64,x' }, {
      actionType: 'job.closeout', resourceId: 'job-1',
    })
    expect(failed()).toEqual([])
    const stale = (await db.sync_queue.toArray()).find((r) => r.body.signature_data === null)
    expect(stale).toMatchObject({ status: QUEUE_STATUS.FAILED, acknowledged: true, superseded: true })

    const before = posts().length
    const o = await retryFailedActions({ ids: [stale.id] })
    expect(o.retried).toBe(0)
    expect(posts().length).toBe(before) // nothing resent
  })

  it('a later note does NOT retire an earlier refused note — they are different notes', async () => {
    await queueOffline('/api/jobs/job-1/notes', { body: 'first' }, { actionType: 'job.note', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/notes', jsonResponse(403, { detail: 'no' }), jsonResponse(201, { id: 'n2' }))
    await syncNow()
    await queueAction('POST', '/api/jobs/job-1/notes', { body: 'second' }, { actionType: 'job.note', resourceId: 'job-1' })
    expect(failed().map((r) => r.action_type)).toEqual(['job.note'])
  })
})

describe('Retry and Discard', () => {
  it('Retry resends exactly the rows passed and reports what happened', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { hours: 2 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/notes', { body: 'keep me failed' }, { actionType: 'job.note', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout',
      jsonResponse(422, { detail: 'invoice missing', missing: ['invoice'] }), jsonResponse(201, { ok: true }))
    route('POST', '/api/jobs/job-1/notes', jsonResponse(403, { detail: 'no' }))
    route('GET', '/api/mobile/job/job-1', jsonResponse(200, { job: { closeout: null } }))
    await syncNow()
    const closeoutRow = failed().find((r) => r.action_type === 'job.closeout')

    const o = await retryFailedActions({ ids: [closeoutRow.id] })
    expect(o).toMatchObject({ retried: 1, sent: 1, refused: 0, pending: 0 })
    expect(failed().map((r) => r.action_type)).toEqual(['job.note']) // untouched
  })

  it('a Retry refused again says why', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { hours: 2 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(422, { detail: 'x', missing: ['signature'] }))
    route('GET', '/api/mobile/job/job-1', jsonResponse(200, { job: { closeout: null } }))
    await syncNow()
    const o = await retryFailedActions({ ids: [failed()[0].id] })
    expect(o).toMatchObject({ retried: 1, sent: 0, refused: 1 })
    expect(o.first.missing).toEqual(['signature'])
  })

  it('a closeout the job has since moved past is retired, not resent', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { hours: 2 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(404, { detail: 'job not found' }))
    await syncNow()
    const later = new Date(Date.now() + 60_000).toISOString()
    route('GET', '/api/mobile/job/job-1', jsonResponse(200, { job: { closeout: { closed_at: later } } }))
    const before = posts().length
    const o = await retryFailedActions({ ids: [failed()[0].id] })
    expect(o).toMatchObject({ retried: 0, superseded: 1 })
    expect(posts().length).toBe(before)
    expect(failed()).toEqual([])
  })

  it('a refused payment is never replayed', async () => {
    expect(isRetryable('invoice.payment')).toBe(false)
    await queueOffline('/api/invoices/inv-1/payments', { amount: 50 }, {
      actionType: 'invoice.payment', resourceId: 'inv-1', conflictIsError: true,
    })
    route('POST', '/api/invoices/inv-1/payments', jsonResponse(409, { detail: 'nothing remaining' }))
    await syncNow()
    const before = posts().length
    const o = await retryFailedActions({ ids: [failed()[0].id] })
    expect(o).toMatchObject({ retried: 0, skipped: 1 })
    expect(posts().length).toBe(before)
  })

  it('Discard deletes exactly the rows passed — not one that arrived meanwhile', async () => {
    await queueOffline('/api/jobs/job-1/notes', { body: 'a' }, { actionType: 'job.note', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/closeout', { hours: 3 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/notes', jsonResponse(403, { detail: 'no' }))
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(404, { detail: 'job not found' }))
    await syncNow()
    const note = failed().find((r) => r.action_type === 'job.note')
    expect(await discardFailedActions({ ids: [note.id] })).toBe(1)
    expect(failed().map((r) => r.action_type)).toEqual(['job.closeout'])
  })
})

describe('the drain does not lose rows', () => {
  it('a row the page died on mid-send (stuck SYNCING) is put back in line and sent', async () => {
    const id = await db.sync_queue.add({
      idempotency_key: 'stuck-1', action_type: 'job.note', resource_id: 'job-1', method: 'POST',
      url: '/api/jobs/job-1/notes', body: { body: 'hi' }, headers: {}, conflict_is_error: false,
      status: QUEUE_STATUS.SYNCING, attempt_count: 0, last_error: null, last_error_code: null,
      last_attempted_at: new Date(Date.now() - 5 * 60_000).toISOString(), created_at: new Date().toISOString(),
    })
    route('POST', '/api/jobs/job-1/notes', jsonResponse(201, { id: 'n1' }))
    await syncNow()
    expect((await db.sync_queue.get(id)).status).toBe(QUEUE_STATUS.SYNCED)
  })

  it('one row the server 5xxs no longer hides the refusals queued behind it', async () => {
    await queueOffline('/api/jobs/job-1/notes', { body: 'a' }, { actionType: 'job.note', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-2/closeout', { hours: 1 }, { actionType: 'job.closeout', resourceId: 'job-2' })
    route('POST', '/api/jobs/job-1/notes', jsonResponse(500, { detail: 'boom' }))
    route('POST', '/api/jobs/job-2/closeout', jsonResponse(404, { detail: 'job not found' }))
    await syncNow()
    expect(failed().map((r) => r.resource_id)).toEqual(['job-2'])
    const note = (await db.sync_queue.toArray()).find((r) => r.action_type === 'job.note')
    expect(note.status).toBe(QUEUE_STATUS.PENDING) // still retrying
  })
})

describe('the drain sends each row once, in order', () => {
  it('two drains started together send a pending row once, not twice', async () => {
    await queueOffline('/api/jobs/job-1/notes', { body: 'once' }, { actionType: 'job.note', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/notes', jsonResponse(201, { id: 'n1' }))
    await Promise.all([syncNow(), syncNow()])
    expect(posts()).toHaveLength(1)
  })

  it('two closeouts queued for one job: the newest goes first, and the older retires once it lands', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(201, { ok: true }))
    await syncNow()
    expect(posts().map(([, init]) => JSON.parse(init.body).at)).toEqual(['10am'])
    expect((await db.sync_queue.toArray()).find((r) => r.body.at === '9am')).toMatchObject({ superseded: true })
  })

  it('three closeouts, the newest refused: the next newest is sent, and it retires the oldest', async () => {
    for (const at of ['9am', '10am', '11am']) {
      await queueOffline('/api/jobs/job-1/closeout', { at }, { actionType: 'job.closeout', resourceId: 'job-1' })
    }
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(422, { detail: 'x', missing: ['signature'] }), jsonResponse(201, { ok: true }))
    await syncNow()
    expect(posts().map(([, init]) => JSON.parse(init.body).at)).toEqual(['11am', '10am'])
    const byAt = Object.fromEntries((await db.sync_queue.toArray()).map((r) => [r.body.at, r]))
    expect([byAt['11am'].status, byAt['10am'].status, byAt['9am'].superseded]).toEqual([QUEUE_STATUS.FAILED, QUEUE_STATUS.SYNCED, true])
  })

  it('the newest one 5xxs: the older ones wait for it, none goes ahead', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(500, { detail: 'boom' }))
    await syncNow()
    expect(posts().map(([, init]) => JSON.parse(init.body).at)).toEqual(['10am'])
    expect((await db.sync_queue.toArray()).map((r) => r.status)).toEqual([QUEUE_STATUS.PENDING, QUEUE_STATUS.PENDING])
  })
  it('a server that is down (502) stops the drain instead of walking the whole queue', async () => {
    for (const j of ['job-1', 'job-2', 'job-3']) {
      await queueOffline(`/api/jobs/${j}/notes`, { body: j }, { actionType: 'job.note', resourceId: j })
    }
    route('POST', /\/api\/jobs\/job-\d\/notes/, jsonResponse(502, { detail: 'bad gateway' }))
    await syncNow()
    expect(posts()).toHaveLength(1)
  })

  it('a send still in flight in THIS page is never resent as "stuck"', async () => {
    let release
    global.fetch = vi.fn(() => new Promise((resolve) => { release = resolve }))
    const first = queueAction('POST', '/api/jobs/job-1/closeout', { hours: 2 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    // Wait until the send has actually claimed the row (one tick was not always
    // enough for the claim's transaction — the test flaked under load).
    let row
    for (let i = 0; i < 200; i++) {
      row = (await db.sync_queue.toArray())[0]
      if (row?.status === QUEUE_STATUS.SYNCING) break
      await new Promise((r) => setTimeout(r, 5))
    }
    expect(row.status).toBe(QUEUE_STATUS.SYNCING)
    // A slow upload on one bar — a drain starts meanwhile (app foregrounded).
    await syncNow()
    expect(global.fetch).toHaveBeenCalledTimes(1)
    release(jsonResponse(201, { ok: true }))
    await first
  })
})

describe('ordering and single delivery (third review)', () => {
  it('an immediate send and a drain started in the same tick send the row once', async () => {
    route('POST', '/api/jobs/job-1/notes', jsonResponse(201, { id: 'n1' }))
    const first = queueAction('POST', '/api/jobs/job-1/notes', { body: 'once' }, { actionType: 'job.note', resourceId: 'job-1' })
    const drain = syncNow()
    await Promise.all([first, drain])
    expect(posts()).toHaveLength(1)
  })

  it('a newer closeout REFUSED on the spot does not retire the older one — Retry still sends it', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(404, { detail: 'job not found' }), jsonResponse(422, { detail: 'x', missing: ['signature'] }), jsonResponse(201, { ok: true }))
    route('GET', '/api/mobile/job/job-1', jsonResponse(200, { job: { closeout: null } }))
    await syncNow() // 9am refused in the background: listed
    await expect(queueAction('POST', '/api/jobs/job-1/closeout', { at: '10am' }, {
      actionType: 'job.closeout', resourceId: 'job-1',
    })).rejects.toMatchObject({ status: 422 }) // redo refused on the spot: replaced nothing
    const nine = failed()[0]
    const o = await retryFailedActions({ ids: [nine.id] })
    expect(o).toMatchObject({ retried: 1, sent: 1, superseded: 0 })
  })

  it('a signed closeout stuck behind a stale token is SENT, not retired, when the redo is refused', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am', signature_data: 'data:image/png;base64,x' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout',
      jsonResponse(401, { detail: 'expired' }),
      jsonResponse(422, { detail: 'x', missing: ['signature'] }),
      jsonResponse(201, { ok: true }))
    await syncNow() // 9am → 401, stays PENDING
    await expect(queueAction('POST', '/api/jobs/job-1/closeout', { at: '10am', signature_data: null }, {
      actionType: 'job.closeout', resourceId: 'job-1',
    })).rejects.toMatchObject({ status: 422 }) // the customer wasn't there to sign again
    await syncNow() // the signed 9am must go out now
    expect(posts().map(([, init]) => JSON.parse(init.body).at)).toEqual(['9am', '10am', '9am'])
    const nine = (await db.sync_queue.toArray()).find((r) => r.body.at === '9am')
    expect(nine.status).toBe(QUEUE_STATUS.SYNCED)
    expect(nine.superseded).toBeUndefined()
  })
  it('two closeouts refused together: both shown; once the newer lands, Retry on the older retires it', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(404, { detail: 'job not found' }), jsonResponse(404, { detail: 'job not found' }), jsonResponse(201, { ok: true }))
    route('GET', '/api/mobile/job/job-1', jsonResponse(200, { job: { closeout: null } }))
    await syncNow()
    const [nine, ten] = failed()
    expect([nine.id < ten.id, failed().length]).toEqual([true, 2])
    expect((await retryFailedActions({ ids: [ten.id] })).sent).toBe(1)
    // The 10am landed → the refused 9am retired itself (never resendable).
    expect(failed()).toEqual([])
    expect((await retryFailedActions({ ids: [nine.id] })).retried).toBe(0)
    // Newest first: the 10am, refused, then the 9am, refused; then Retry's 10am.
    expect(posts().map(([, init]) => JSON.parse(init.body).at)).toEqual(['10am', '9am', '10am'])
  })
  it("a job's arrival that 500s holds back that job's closeout for the pass", async () => {
    await queueOffline('/api/mobile/jobs/job-1/arrived', {}, { actionType: 'job.arrived', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/closeout', { hours: 1 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/mobile/jobs/job-1/arrived', jsonResponse(500, { detail: 'boom' }))
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(201, { ok: true }))
    await syncNow()
    expect(posts().map(([url]) => url)).toEqual(['/api/mobile/jobs/job-1/arrived'])
  })

  it('a stale token (401) stops the drain — nothing newer overtakes the row', async () => {
    await queueOffline('/api/jobs/job-1/notes', { body: 'a' }, { actionType: 'job.note', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-2/notes', { body: 'b' }, { actionType: 'job.note', resourceId: 'job-2' })
    route('POST', /\/api\/jobs\/job-\d\/notes/, jsonResponse(401, { detail: 'expired' }))
    await syncNow()
    expect(posts()).toHaveLength(1)
  })

  it('a Retry tapped during a long drain still goes out, right after it', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { hours: 1 }, { actionType: 'job.closeout', resourceId: 'job-1' })
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(404, { detail: 'job not found' }), jsonResponse(201, { ok: true }))
    route('GET', '/api/mobile/job/job-1', jsonResponse(200, { job: { closeout: null } }))
    await syncNow()
    const refused = failed()[0]

    // A slow unrelated send keeps a drain busy.
    await queueOffline('/api/jobs/job-9/notes', { body: 'slow' }, { actionType: 'job.note', resourceId: 'job-9' })
    let release
    const routed = global.fetch.getMockImplementation()
    global.fetch.mockImplementation((url, init) => (
      url === '/api/jobs/job-9/notes' ? new Promise((resolve) => { release = () => resolve(jsonResponse(201, {})) }) : routed(url, init)
    ))
    const busy = syncNow()
    await new Promise((r) => setTimeout(r, 0))
    setTimeout(() => release(), 300)
    const o = await retryFailedActions({ ids: [refused.id] })
    await busy
    expect(o).toMatchObject({ retried: 1, sent: 1, pending: 0 })
  })
})

describe('latest wins at the claim (fourth review)', () => {
  it('an older closeout stuck PENDING is retired, not sent, once a newer one landed on the spot', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    // Reconnect with a stale token: the 9am replay 401s and stays PENDING.
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(401, { detail: 'expired' }), jsonResponse(201, { ok: true }))
    await syncNow()
    // The job still reads on_site, so the tech closes out again — it lands.
    await queueAction('POST', '/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    // The next drain must not land 9am after it.
    await syncNow()
    expect(posts().map(([, init]) => JSON.parse(init.body).at)).toEqual(['9am', '10am'])
    const nine = (await db.sync_queue.toArray()).find((r) => r.body.at === '9am')
    expect(nine).toMatchObject({ acknowledged: true, superseded: true })
    expect(failed()).toEqual([])
  })
})

describe('groups — what a landed write retires, and what it must not', () => {
  it('an arrival that landed retires an older "on my way" stuck behind a stale token', async () => {
    await queueOffline('/api/mobile/jobs/job-1/en-route', {}, { actionType: 'job.en_route', resourceId: 'job-1' })
    route('POST', '/api/mobile/jobs/job-1/en-route', jsonResponse(401, { detail: 'expired' }))
    route('POST', '/api/mobile/jobs/job-1/arrived', jsonResponse(200, { ok: true }))
    await syncNow() // en_route → 401, PENDING
    await queueAction('POST', '/api/mobile/jobs/job-1/arrived', {}, { actionType: 'job.arrived', resourceId: 'job-1' })
    await syncNow()
    const enRoutePosts = posts().filter(([url]) => url.endsWith('/en-route'))
    expect(enRoutePosts).toHaveLength(1) // never replayed after the arrival
    expect(failed()).toEqual([]) // and no un-fixable "didn't send" row
  })

  it('two address fixes are both sent — the second one expects the first', async () => {
    await queueOffline('/api/mobile/jobs/job-1/site', { address: '124 Main', expected_address: '123 Main' }, { actionType: 'job.site_fix', resourceId: 'job-1' })
    await queueOffline('/api/mobile/jobs/job-1/site', { address: '125 Maine', expected_address: '124 Main' }, { actionType: 'job.site_fix', resourceId: 'job-1' })
    route('PATCH', '/api/mobile/jobs/job-1/site', jsonResponse(200, { ok: true }))
    route('POST', '/api/mobile/jobs/job-1/site', jsonResponse(200, { ok: true }))
    await syncNow()
    expect(global.fetch.mock.calls.filter(([url]) => url.endsWith('/site'))).toHaveLength(2)
  })

  it('purge keeps a landed closeout that an older unsent one still relies on', async () => {
    const { purgeOldSynced } = await import('../useOfflineSync')
    const old = new Date(Date.now() - 10 * 86400_000).toISOString()
    const base = { action_type: 'job.closeout', resource_id: 'job-1', method: 'POST', url: '/api/jobs/job-1/closeout', headers: {} }
    await db.sync_queue.add({ ...base, idempotency_key: '9am', body: { at: '9am' }, status: QUEUE_STATUS.PENDING, created_at: old, last_attempted_at: old })
    await db.sync_queue.add({ ...base, idempotency_key: '10am', body: { at: '10am' }, status: QUEUE_STATUS.SYNCED, created_at: old, last_attempted_at: old })
    await purgeOldSynced()
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(201, { ok: true }))
    await syncNow()
    expect(posts()).toHaveLength(0) // 9am still retired by the kept 10am
  })
})

describe('the queue does not grow forever', () => {
  it('purge drops old refusals the tech saw or that were replaced, never an unseen one', async () => {
    const { purgeOldSynced } = await import('../useOfflineSync')
    const old = new Date(Date.now() - 10 * 86400_000).toISOString()
    const recent = new Date().toISOString()
    const base = { action_type: 'job.note', resource_id: 'job-1', method: 'POST', url: '/x', body: {}, headers: {}, status: QUEUE_STATUS.FAILED }
    await db.sync_queue.bulkAdd([
      { ...base, idempotency_key: 'old-seen', acknowledged: true, created_at: old, last_attempted_at: old },
      { ...base, idempotency_key: 'old-unseen', created_at: old, last_attempted_at: old },
      { ...base, idempotency_key: 'recent-seen', acknowledged: true, created_at: recent, last_attempted_at: recent },
    ])
    await purgeOldSynced()
    const keys = (await db.sync_queue.toArray()).map((r) => r.idempotency_key).sort()
    expect(keys).toEqual(['old-unseen', 'recent-seen'])
  })
})

// Sixth review. The fetch here HOLDS each send until the test releases it, so
// two sends that overlap on the wire can be seen overlapping — an instant fetch
// proves only the order sends start in, never the order the server gets them.
describe('one write of a kind per job on the wire (sixth review)', () => {
  let held = [], started, onWire, maxOnWire
  function holdingFetch(firstReplies = []) {
    held = []; started = []; onWire = 0; maxOnWire = 0
    const replies = [...firstReplies]
    global.fetch = vi.fn((url, init) => {
      started.push(JSON.parse(init.body).at)
      if (replies.length) return Promise.resolve(replies.shift())
      onWire += 1; maxOnWire = Math.max(maxOnWire, onWire)
      return new Promise((resolve) => {
        held.push({ at: JSON.parse(init.body).at, release: (resp) => { onWire -= 1; resolve(resp) } })
      })
    })
  }
  async function until(check) {
    for (let i = 0; i < 400; i++) {
      if (await check()) return
      await new Promise((r) => setTimeout(r, 5))
    }
    throw new Error('timed out waiting')
  }
  const rowAt = async (at) => (await db.sync_queue.toArray()).find((r) => r.body?.at === at)

  // A test that fails mid-way must not leave a drain hanging on a held send
  // for the next one: let everything still held land, and wait for quiet.
  afterEach(async () => {
    const { syncing } = (() => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      try { return useOfflineSync() } finally { warn.mockRestore() }
    })()
    for (let i = 0; i < 200 && (held.length || syncing.value); i++) {
      held.splice(0).forEach((h) => h.release(jsonResponse(201, {})))
      await new Promise((r) => setTimeout(r, 5))
    }
  })

  it('the 9am stuck behind a stale token never goes out while the 10am redo is uploading', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    holdingFetch([jsonResponse(401, { detail: 'expired' })])
    await syncNow() // 9am → 401, PENDING
    const ten = queueAction('POST', '/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await until(async () => (await rowAt('10am'))?.status === QUEUE_STATUS.SYNCING)
    // App foregrounded mid-upload. Not awaited: a drain that DID send the 9am
    // would sit on it, and this must fail on what was sent, not time out.
    const mid = syncNow()
    await Promise.race([mid, new Promise((r) => setTimeout(r, 300))])
    expect(started).toEqual(['9am', '10am']) // no second 9am beside the 10am
    held.shift().release(jsonResponse(201, { ok: true }))
    await Promise.all([ten, mid])
    // The 10am landed; the drain it starts retires the 9am instead of sending it.
    await until(async () => (await rowAt('9am'))?.status === QUEUE_STATUS.FAILED)
    expect(await rowAt('9am')).toMatchObject({ acknowledged: true, superseded: true })
    expect(started).toEqual(['9am', '10am'])
    expect(failed()).toEqual([])
  })

  it('a redo tapped while the drain uploads the older one waits, then goes out after it', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    holdingFetch()
    const drain = syncNow()
    await until(async () => (await rowAt('9am'))?.status === QUEUE_STATUS.SYNCING)
    // Raced, not awaited: a redo that went straight out would sit on the wire.
    const r = await Promise.race([
      queueAction('POST', '/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' }),
      new Promise((resolve) => setTimeout(() => resolve('sent beside the 9am'), 300)),
    ])
    expect(r).toMatchObject({ queued: true })
    expect(started).toEqual(['9am'])
    held.shift().release(jsonResponse(201, { ok: true }))
    await until(() => started.length === 2)
    held.shift().release(jsonResponse(201, { ok: true }))
    await drain
    await until(async () => (await rowAt('10am'))?.status === QUEUE_STATUS.SYNCED)
    expect(started).toEqual(['9am', '10am'])
    expect(maxOnWire).toBe(1) // never two closeouts for the job at once
    expect((await rowAt('9am')).status).toBe(QUEUE_STATUS.SYNCED)
  })

  it('writes of different kinds, or for different jobs, are not held up', async () => {
    await queueOffline('/api/jobs/job-2/closeout', { at: 'job-2' }, { actionType: 'job.closeout', resourceId: 'job-2' })
    holdingFetch()
    const one = queueAction('POST', '/api/jobs/job-1/closeout', { at: 'job-1' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await until(() => started.length === 1)
    const drain = syncNow()
    await until(() => started.length === 2) // job-2's closeout went out beside job-1's
    held.splice(0).forEach((h) => h.release(jsonResponse(201, { ok: true })))
    await Promise.all([one, drain])
    expect(started.sort()).toEqual(['job-1', 'job-2'])
  })
})

// Seventh review. The usual dead-zone failure is not "never arrived" but
// "landed, answer lost". The server here is shaped like prod: it caches a 2xx
// under the Idempotency-Key and answers a replay from that cache WITHOUT
// running it, and a closeout that runs becomes the current one.
describe('a newer write that landed while its answer was lost still wins (seventh review)', () => {
  function closeoutServer({ first401 = new Set(), loseAnswerFor = new Set() } = {}) {
    const s = { current: null, executed: [], cache: new Map() }
    global.fetch = vi.fn(async (url, init) => {
      const key = init.headers['Idempotency-Key']
      const { at } = JSON.parse(init.body)
      if (first401.delete(at)) return jsonResponse(401, { detail: 'expired' })
      if (s.cache.has(key)) return s.cache.get(key)
      s.executed.push(at); s.current = at
      const resp = jsonResponse(201, { ok: true }); s.cache.set(key, resp)
      if (loseAnswerFor.delete(at)) throw new TypeError('Failed to fetch')
      return resp
    })
    return s
  }

  it('9am stuck on a stale token, 10am redo lands but its answer is lost: the server ends on the 10am', async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1', conflictIsError: true })
    const server = closeoutServer({ first401: new Set(['9am']), loseAnswerFor: new Set(['10am']) })
    await syncNow() // 9am → 401
    await queueAction('POST', '/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1', conflictIsError: true })
    await syncNow() // next bar of signal
    expect(server.executed).toEqual(['10am'])
    expect(server.current).toBe('10am')
    expect((await db.sync_queue.toArray()).find((r) => r.body.at === '9am')).toMatchObject({ superseded: true })
    expect(failed()).toEqual([])
  })

  it('same, but the page died mid-upload of the 10am (recovered next page): the server ends on the 10am', async () => {
    // Both rows as the previous page left them, times in the order they happened:
    // the 9am queued at 20 min ago and 401'd; the 10am queued and sent at 10 min
    // ago — it ran on the server, and the page died before the answer came back.
    const ago = (m) => new Date(Date.now() - m * 60_000).toISOString()
    const base = {
      action_type: 'job.closeout', resource_id: 'job-1', method: 'POST', url: '/api/jobs/job-1/closeout',
      headers: {}, conflict_is_error: true, last_error_missing: null,
    }
    await db.sync_queue.add({
      ...base, idempotency_key: 'nine-key', body: { at: '9am' }, status: QUEUE_STATUS.PENDING, attempt_count: 1,
      last_error: 'unauthorized_will_retry', last_error_code: 401, created_at: ago(20), last_attempted_at: ago(20),
    })
    await db.sync_queue.add({
      ...base, idempotency_key: 'ten-key', body: { at: '10am' }, status: QUEUE_STATUS.SYNCING, attempt_count: 0,
      last_error: null, last_error_code: null, created_at: ago(10), last_attempted_at: ago(10),
    })
    const server = closeoutServer()
    server.executed.push('10am'); server.current = '10am'
    server.cache.set('ten-key', jsonResponse(201, { ok: true }))
    await syncNow()
    expect(server.executed).toEqual(['10am'])
    expect(server.current).toBe('10am')
  })
})

// Eighth review. "May have sent" is an outcome the phone does not know — it
// must hold older writes of its kind back like a PENDING one does, and an
// answer lost to the network is the same "may have run" as a page that died.
describe('an unknown outcome is never guessed at (eighth review)', () => {
  const ago = (m) => new Date(Date.now() - m * 60_000).toISOString()
  const closeout = {
    action_type: 'job.closeout', resource_id: 'job-1', method: 'POST', url: '/api/jobs/job-1/closeout',
    headers: {}, conflict_is_error: true, last_error_missing: null,
  }

  afterEach(() => { vi.useRealTimers() })

  it('a newer closeout that "may have sent" holds the older one back — and Retry on it never lets the older one win', async () => {
    const D3 = 3 * 1440
    await db.sync_queue.add({
      ...closeout, idempotency_key: 'nine', body: { at: '9am' }, status: QUEUE_STATUS.PENDING, attempt_count: 1,
      last_error: 'unauthorized_will_retry', last_error_code: 401, created_at: ago(D3 + 20), last_attempted_at: ago(D3 + 20),
    })
    const tenId = await db.sync_queue.add({
      ...closeout, idempotency_key: 'ten', body: { at: '10am' }, status: QUEUE_STATUS.SYNCING, attempt_count: 0,
      last_error: null, last_error_code: null, created_at: ago(D3 + 10), last_attempted_at: ago(D3 + 10),
    })
    // The 10am RAN (the page died before its answer) three days ago — a weekend;
    // the server's cache of that answer is long gone.
    const s = { executed: ['10am'], current: '10am', closedAt: ago(D3 + 9) }
    global.fetch = vi.fn(async (url, init = {}) => {
      if ((init.method || 'GET') === 'GET') return jsonResponse(200, { job: { closeout: { closed_at: s.closedAt } } })
      const { at } = JSON.parse(init.body)
      s.executed.push(at); s.current = at; s.closedAt = new Date().toISOString()
      return jsonResponse(201, { ok: true })
    })
    await syncNow()
    expect(s.executed).toEqual(['10am']) // the 9am waits for the tech to check the 10am
    expect(failed().map((r) => [r.action_type, r.uncertain])).toEqual([['job.closeout', true]])
    const o = await retryFailedActions({ ids: [tenId] })
    expect(o).toMatchObject({ sent: 0, superseded: 1, supersededBy: 'closed_since' })
    await syncNow()
    expect(s.executed).toEqual(['10am'])
    expect(s.current).toBe('10am')
    expect((await db.sync_queue.toArray()).find((r) => r.body.at === '9am')).toMatchObject({ superseded: true })
  })

  // The server here caches a 2xx under the Idempotency-Key for 24 h and answers
  // a replay from it; after that a replay runs again. Its 120 s duplicate
  // window is long closed too. Only Date is faked.
  function payServer() {
    const s = { recorded: [], cache: new Map(), loseNext: true }
    global.fetch = vi.fn(async (url, init) => {
      const key = init.headers['Idempotency-Key']
      const hit = s.cache.get(key)
      if (hit && Date.now() - hit.at < 86400_000) return hit.resp
      s.recorded.push(JSON.parse(init.body))
      const resp = jsonResponse(201, { id: `p${s.recorded.length}` })
      s.cache.set(key, { resp, at: Date.now() })
      if (s.loseNext) { s.loseNext = false; throw new TypeError('Failed to fetch') }
      return resp
    })
    return s
  }
  for (const [label, gapH, recorded, strip] of [['an hour later', 1, 1, 0], ['Friday 4:55 pm → Monday 8 am', 63, 1, 1]]) {
    it(`a $500 cash payment whose answer was lost, next synced ${label}: recorded once`, async () => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date('2026-09-11T21:55:00Z'))
      const s = payServer()
      const r = await queueAction('POST', '/api/invoices/inv-1/payments', { amount: 500, method: 'cash', reference: null }, {
        actionType: 'invoice.payment', resourceId: 'inv-1', conflictIsError: true,
      })
      expect(r).toMatchObject({ queued: true })
      vi.setSystemTime(new Date(Date.now() + gapH * 3600_000))
      await syncNow()
      expect(s.recorded).toHaveLength(recorded)
      expect(failed()).toHaveLength(strip)
      if (strip) expect(failed()[0]).toMatchObject({ uncertain: true, amount: '$500.00 cash' })
    })
  }

  it('a lost answer is stamped once — the first attempt that may have run is the one the server cached', async () => {
    global.fetch = vi.fn(async () => { throw new TypeError('Failed to fetch') })
    await queueAction('POST', '/api/jobs/job-1/notes', { body: 'x' }, { actionType: 'job.note', resourceId: 'job-1' })
    const first = (await db.sync_queue.toArray())[0]
    expect(first.first_maybe_ran_at).toBe(first.last_attempted_at)
    await new Promise((r) => setTimeout(r, 5))
    await syncNow()
    const again = (await db.sync_queue.toArray())[0]
    expect(again.last_attempted_at > first.last_attempted_at).toBe(true)
    expect(again.first_maybe_ran_at).toBe(first.first_maybe_ran_at)
  })

  it('Retry on a "may have sent" row is the check it waited for: it goes out, and is not held again', async () => {
    const id = await db.sync_queue.add({
      idempotency_key: 'part-1', action_type: 'job.part_used', resource_id: 'job-1', method: 'POST',
      url: '/api/mobile/jobs/job-1/parts-used', body: { sku: 'S1' }, headers: {}, conflict_is_error: false,
      status: QUEUE_STATUS.PENDING, attempt_count: 1, last_error: 'Failed to fetch', last_error_code: null,
      created_at: ago(3000), last_attempted_at: ago(3000), first_maybe_ran_at: ago(3000),
    })
    route('POST', '/api/mobile/jobs/job-1/parts-used', jsonResponse(201, { id: 'pu1' }))
    await syncNow()
    expect(posts()).toHaveLength(0)
    expect(await db.sync_queue.get(id)).toMatchObject({ status: QUEUE_STATUS.FAILED, uncertain: true })
    expect((await retryFailedActions({ ids: [id] })).sent).toBe(1)
  })

  it("Retry on a refused closeout is not retired by this phone's own older one landing after it", async () => {
    await queueOffline('/api/jobs/job-1/closeout', { at: '9am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    await queueOffline('/api/jobs/job-1/closeout', { at: '10am' }, { actionType: 'job.closeout', resourceId: 'job-1' })
    const s = { current: null, closedAt: null }
    let refuseNext = true
    global.fetch = vi.fn(async (url, init = {}) => {
      if ((init.method || 'GET') === 'GET') return jsonResponse(200, { job: { closeout: s.closedAt ? { closed_at: s.closedAt } : null } })
      const { at } = JSON.parse(init.body)
      if (refuseNext) { refuseNext = false; return jsonResponse(422, { detail: 'x', missing: ['invoice'] }) }
      s.current = at; s.closedAt = new Date(Date.now() + 1000).toISOString()
      return jsonResponse(201, { ok: true })
    })
    await syncNow() // 10am refused (no invoice yet); the signed 9am goes out after it
    expect(s.current).toBe('9am')
    const ten = failed()[0]
    // The office creates the invoice; the tech taps Retry on the 10am.
    expect(await retryFailedActions({ ids: [ten.id] })).toMatchObject({ sent: 1, superseded: 0 })
    expect(s.current).toBe('10am')
  })

  it('purge keeps a retired closeout that an older unsent one still relies on', async () => {
    const { purgeOldSynced } = await import('../useOfflineSync')
    const old = ago(10 * 1440)
    await db.sync_queue.add({ ...closeout, idempotency_key: '9am', body: { at: '9am' }, status: QUEUE_STATUS.PENDING, created_at: old, last_attempted_at: old })
    await db.sync_queue.add({
      ...closeout, idempotency_key: '10am', body: { at: '10am' }, status: QUEUE_STATUS.FAILED,
      acknowledged: true, superseded: true, created_at: old, last_attempted_at: old,
    })
    await purgeOldSynced()
    route('POST', '/api/jobs/job-1/closeout', jsonResponse(201, { ok: true }))
    await syncNow()
    expect(posts()).toHaveLength(0) // 9am still retired by the kept 10am
  })
})

describe('a send the page died on is resent only when that cannot record it twice (sixth review)', () => {
  function stuck(key, actionType, url, body, attemptedAgoMs) {
    const t = new Date(Date.now() - attemptedAgoMs).toISOString()
    return db.sync_queue.add({
      idempotency_key: key, action_type: actionType, resource_id: 'inv-1', method: 'POST', url, body,
      headers: {}, conflict_is_error: true, status: QUEUE_STATUS.SYNCING, attempt_count: 0,
      last_error: null, last_error_code: null, last_attempted_at: t, created_at: t,
    })
  }

  it('a payment is never resent on its own — at any age — and is shown as "may have sent", with its amount', async () => {
    const days = await stuck('pay-1', 'invoice.payment', '/api/invoices/inv-1/payments', { amount: 120, method: 'cash' }, 3 * 86400_000)
    const minutes = await stuck('pay-2', 'invoice.payment', '/api/invoices/inv-1/payments', { amount: 40, method: 'check' }, 5 * 60_000)
    route('POST', '/api/invoices/inv-1/payments', jsonResponse(201, { id: 'p1' }))
    await syncNow()
    expect(posts()).toHaveLength(0)
    for (const id of [days, minutes]) {
      expect(await db.sync_queue.get(id)).toMatchObject({ status: QUEUE_STATUS.FAILED, uncertain: true, acknowledged: false })
    }
    const rows = failed()
    expect(rows.map((r) => r.amount)).toEqual(['$120.00 cash', '$40.00 check'])
    expect(describeQueuedRefusal(rows[0])).toBe('The phone never got a clear answer while sending it — it may already be on the server.')
  })

  it('past the server\'s 24 h replay window, any write waits for the tech instead of replaying', async () => {
    const id = await stuck('note-1', 'job.note', '/api/jobs/job-1/notes', { body: 'hi' }, 3 * 86400_000)
    route('POST', '/api/jobs/job-1/notes', jsonResponse(201, { id: 'n1' }))
    await syncNow()
    expect(posts()).toHaveLength(0)
    expect(await db.sync_queue.get(id)).toMatchObject({ status: QUEUE_STATUS.FAILED, uncertain: true })
    expect(describeQueuedRefusal(failed()[0])).toContain('Check the job before you Retry.')
    // Retry after checking resends it, and it is no longer "uncertain".
    await retryFailedActions({ ids: [id] })
    expect(await db.sync_queue.get(id)).toMatchObject({ status: QUEUE_STATUS.SYNCED, uncertain: false })
  })

  it("a payment refusal keeps the server's code — 'duplicate_payment' means the money IS recorded", async () => {
    await queueOffline('/api/invoices/inv-1/payments', { amount: 50, method: 'cash' }, {
      actionType: 'invoice.payment', resourceId: 'inv-1', conflictIsError: true,
    })
    route('POST', '/api/invoices/inv-1/payments',
      jsonResponse(409, { detail: { code: 'duplicate_payment', message: 'an identical cash payment was recorded moments ago' } }))
    await syncNow()
    expect(failed()[0]).toMatchObject({ reason: 'duplicate_payment', amount: '$50.00 cash', http_status: 409 })
  })
})

describe('describing a refusal in plain words', () => {
  it('names the action and the reason a tech can act on', () => {
    expect(describeQueuedAction('job.closeout')).toBe('Closeout')
    expect(describeQueuedAction('something.new')).toBe('A change')
    expect(describeQueuedRefusal({ http_status: 404 })).toBe('That job no longer exists on the server.')
    expect(describeQueuedRefusal({ action_type: 'job.closeout', http_status: 404 })).toBe('That job no longer exists on the server.')
    expect(describeQueuedRefusal({ action_type: 'invoice.payment', http_status: 404 })).toBe('That invoice no longer exists on the server.')
    expect(describeQueuedRefusal({ action_type: 'parts.status', http_status: 404 })).toBe('That part request no longer exists on the server.')
    expect(describeQueuedRefusal({ http_status: 403 })).toContain('not allowed')
    expect(describeQueuedRefusal({ http_status: 400, error: 'quantity must be positive' }))
      .toBe('The server said: quantity must be positive')
    expect(describeQueuedRefusal({ http_status: 400, error: 'HTTP 400' })).toBe('The server refused it.')
  })
})
