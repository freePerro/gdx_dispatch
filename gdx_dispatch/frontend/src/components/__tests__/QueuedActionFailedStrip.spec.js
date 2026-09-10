/**
 * QueuedActionFailedStrip (#528) — the reader for refused offline writes.
 * Scoped to one job inside that job's screen, phone-wide on Today; says what
 * didn't send and why; actions are PER ROW on the row the tech is looking at
 * (a bulk Retry on Today replayed every stale body on the phone, payments
 * included; a bulk Discard deleted a refusal that arrived during the confirm).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// vi.mock is hoisted above everything, so the mocks live inside the factory and
// are read back through the mocked module below.
vi.mock('../../composables/useOfflineSync', async (importOriginal) => {
  const real = await importOriginal()
  const { ref } = await import('vue')
  return {
    failedActions: ref([]),
    refreshFailedActions: vi.fn(),
    retryFailedActions: vi.fn(),
    discardFailedActions: vi.fn(),
    describeQueuedAction: real.describeQueuedAction,
    describeQueuedRefusal: real.describeQueuedRefusal,
    isRetryable: real.isRetryable,
    // A mount of the composable starts a drain; the strip must not make one.
    useOfflineSync: vi.fn(),
  }
})
import * as queue from '../../composables/useOfflineSync'

const { failedActions, retryFailedActions, discardFailedActions, refreshFailedActions, useOfflineSync } = queue
const confirmAsync = vi.fn()
vi.mock('../../composables/useDestructiveConfirm', () => ({ useDestructiveConfirm: () => ({ confirmAsync }) }))
const toastAdd = vi.fn()
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

import QueuedActionFailedStrip from '../QueuedActionFailedStrip.vue'

const CLOSEOUT = {
  id: 1, action_type: 'job.closeout', resource_id: 'job-1', http_status: 422,
  error: 'completion requirements unmet', missing: ['signature'], created_at: '2026-09-10T20:30:00Z',
}
const NOTE_OTHER_JOB = {
  id: 2, action_type: 'job.note', resource_id: 'job-2', http_status: 404,
  error: 'job not found', missing: [], created_at: '2026-09-10T20:31:00Z',
}
// A refusal with no machine code: only proves THIS copy was not added. (The
// server's duplicate-check-number 409 is exactly this shape.)
const PAYMENT = {
  id: 3, action_type: 'invoice.payment', resource_id: 'inv-1', http_status: 409, reason: null,
  error: "a payment with reference '1234' is already recorded on this invoice", missing: [],
  amount: '$120.00 check', created_at: '2026-09-10T20:32:00Z',
}
// The server's `duplicate_payment` 409: the money IS on the invoice.
const DUP_PAYMENT = {
  ...PAYMENT, id: 4, reason: 'duplicate_payment', amount: '$50.00 cash',
  error: 'an identical cash payment of 50.00 was recorded moments ago',
}

beforeEach(() => {
  failedActions.value = []
  refreshFailedActions.mockReset()
  useOfflineSync.mockReset()
  retryFailedActions.mockReset()
  discardFailedActions.mockReset()
  confirmAsync.mockReset()
  toastAdd.mockReset()
})

describe('QueuedActionFailedStrip', () => {
  it('renders nothing when nothing was refused', () => {
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    expect(w.find('[data-testid="queued-failed-strip"]').exists()).toBe(false)
  })

  it('reads what is on the phone without starting a drain of its own', () => {
    mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    expect(refreshFailedActions).toHaveBeenCalledTimes(1)
    expect(useOfflineSync).not.toHaveBeenCalled()
  })

  it("inside a job, shows only that job's refusals — in words", () => {
    failedActions.value = [CLOSEOUT, NOTE_OTHER_JOB]
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    expect(w.get('[data-testid="queued-failed-count"]').text()).toBe("1 change didn't send")
    const items = w.findAll('[data-testid="queued-failed-item"]')
    expect(items).toHaveLength(1)
    expect(items[0].text()).toContain('Closeout — Still needs: customer signature')
    expect(w.find('[data-testid="queued-failed-closeout-hint"]').exists()).toBe(true)
  })

  it('on Today (no job), shows every job', () => {
    failedActions.value = [CLOSEOUT, NOTE_OTHER_JOB]
    const w = mount(QueuedActionFailedStrip)
    expect(w.get('[data-testid="queued-failed-count"]').text()).toBe("2 changes didn't send")
    expect(w.text()).toContain('Note — That job no longer exists on the server.')
  })

  it('Retry on a row resends exactly that row and says so', async () => {
    failedActions.value = [CLOSEOUT, NOTE_OTHER_JOB]
    retryFailedActions.mockResolvedValue({ retried: 1, sent: 1, refused: 0, pending: 0, superseded: 0, skipped: 0, first: null })
    const w = mount(QueuedActionFailedStrip)
    await w.findAll('[data-testid="queued-failed-retry"]')[1].trigger('click')
    await flushPromises()
    expect(retryFailedActions).toHaveBeenCalledWith({ ids: [2] })
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success', summary: 'Note sent' }))
  })

  it('Retry refused again says why, not a silent blink', async () => {
    failedActions.value = [CLOSEOUT]
    retryFailedActions.mockResolvedValue({
      retried: 1, sent: 0, refused: 1, pending: 0, superseded: 0, skipped: 0,
      first: { action_type: 'job.closeout', http_status: 422, error: 'x', missing: ['signature'] },
    })
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    await w.get('[data-testid="queued-failed-retry"]').trigger('click')
    await flushPromises()
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'warn', summary: 'Still refused', detail: 'Closeout: Still needs: customer signature',
    }))
  })

  it('a stale closeout is retired with an explanation, not resent', async () => {
    failedActions.value = [CLOSEOUT]
    retryFailedActions.mockResolvedValue({ retried: 0, sent: 0, refused: 0, pending: 0, superseded: 1, skipped: 0, first: null })
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    await w.get('[data-testid="queued-failed-retry"]').trigger('click')
    await flushPromises()
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Already closed out' }))
  })

  it('an older closeout with a newer one on the phone says so — not "already closed out"', async () => {
    failedActions.value = [CLOSEOUT]
    retryFailedActions.mockResolvedValue({ retried: 0, sent: 0, refused: 0, pending: 0, superseded: 1, supersededBy: 'newer_on_phone', skipped: 0, first: null })
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    await w.get('[data-testid="queued-failed-retry"]').trigger('click')
    await flushPromises()
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'A newer closeout exists' }))
  })

  it('a Retry still in flight with signal says "still sending", not "waiting for signal"', async () => {
    failedActions.value = [CLOSEOUT]
    retryFailedActions.mockResolvedValue({ retried: 1, sent: 0, refused: 0, pending: 1, superseded: 0, skipped: 0, first: null, online: true })
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    await w.get('[data-testid="queued-failed-retry"]').trigger('click')
    await flushPromises()
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Still sending' }))
  })

  it('a refused payment offers no Retry, shows the amount, and asks the office to look — never says the money is missing', () => {
    failedActions.value = [PAYMENT]
    const w = mount(QueuedActionFailedStrip)
    expect(w.find('[data-testid="queued-failed-retry"]').exists()).toBe(false)
    expect(w.get('[data-testid="queued-failed-amount"]').text()).toBe('$120.00 check')
    const note = w.get('[data-testid="queued-failed-office"]').text()
    expect(note).toBe("This phone's copy was not added to the invoice. Tell the office before you delete it here.")
    expect(note).not.toContain('not on the invoice')
  })

  // The server's duplicate check is invoice + amount + method within its last
  // 120 s, so two separate $200 cash payments replayed back to back collide:
  // neither "missing" nor "it is on the invoice" may be said flat.
  it("a 'duplicate_payment' refusal: an identical one is on the invoice — never 'this one is', never safe to just delete", async () => {
    failedActions.value = [DUP_PAYMENT]
    confirmAsync.mockResolvedValue(false)
    const w = mount(QueuedActionFailedStrip)
    const note = w.get('[data-testid="queued-failed-office"]').text()
    expect(note).toBe('An identical payment is already on the invoice. If this was a second, separate payment, tell the office before you delete it here.')
    expect(note).not.toMatch(/not (on|added to) the invoice|it is on the invoice/)
    await w.get('[data-testid="queued-failed-discard"]').trigger('click')
    await flushPromises()
    const { message } = confirmAsync.mock.calls[0][0]
    expect(message).toContain('second, separate payment')
    expect(message).toContain('only record')
    expect(message).not.toContain('takes nothing off')
  })

  it('a payment the phone lost track of mid-send says it may already be on the invoice', () => {
    failedActions.value = [{ ...PAYMENT, id: 5, http_status: null, error: 'no_clear_answer', uncertain: true }]
    const w = mount(QueuedActionFailedStrip)
    expect(w.get('[data-testid="queued-failed-office"]').text()).toContain('may already be on the invoice')
    expect(w.text()).toContain('The phone never got a clear answer while sending it')
    expect(w.text()).not.toContain('Retry.') // no Retry is offered on a payment, so none is suggested
  })

  it("deleting a refused payment warns it may be the only record — no 'Retry first'", async () => {
    failedActions.value = [PAYMENT]
    confirmAsync.mockResolvedValue(false)
    const w = mount(QueuedActionFailedStrip)
    await w.get('[data-testid="queued-failed-discard"]').trigger('click')
    await flushPromises()
    const { message } = confirmAsync.mock.calls[0][0]
    expect(message).toContain('only record')
    expect(message).not.toContain('Retry')
  })

  it('a Retry the server 5xxs says server trouble, not "going through in a moment"', async () => {
    failedActions.value = [CLOSEOUT]
    retryFailedActions.mockResolvedValue({ retried: 1, sent: 0, refused: 0, pending: 1, superseded: 0, skipped: 0, first: null, online: true, serverError: true })
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    await w.get('[data-testid="queued-failed-retry"]').trigger('click')
    await flushPromises()
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Server trouble' }))
  })

  it('Discard asks first, naming the only copy — and does nothing on No', async () => {
    failedActions.value = [CLOSEOUT]
    confirmAsync.mockResolvedValue(false)
    const w = mount(QueuedActionFailedStrip, { props: { jobId: 'job-1' } })
    await w.get('[data-testid="queued-failed-discard"]').trigger('click')
    await flushPromises()
    expect(confirmAsync).toHaveBeenCalledWith(expect.objectContaining({
      header: 'Delete this closeout from the phone?',
      message: expect.stringContaining('only on this phone'),
    }))
    expect(discardFailedActions).not.toHaveBeenCalled()
  })

  it('Discard on Yes deletes exactly that row', async () => {
    failedActions.value = [CLOSEOUT, NOTE_OTHER_JOB]
    confirmAsync.mockResolvedValue(true)
    discardFailedActions.mockResolvedValue(1)
    const w = mount(QueuedActionFailedStrip)
    await w.findAll('[data-testid="queued-failed-discard"]')[0].trigger('click')
    await flushPromises()
    expect(discardFailedActions).toHaveBeenCalledWith({ ids: [1] })
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Closeout deleted from this phone' }))
  })

  it('more than three collapse behind "Show all"', async () => {
    failedActions.value = [1, 2, 3, 4, 5].map((id) => ({ ...NOTE_OTHER_JOB, id }))
    const w = mount(QueuedActionFailedStrip)
    expect(w.findAll('[data-testid="queued-failed-item"]')).toHaveLength(3)
    await w.get('[data-testid="queued-failed-show-all"]').trigger('click')
    expect(w.findAll('[data-testid="queued-failed-item"]')).toHaveLength(5)
  })
})
