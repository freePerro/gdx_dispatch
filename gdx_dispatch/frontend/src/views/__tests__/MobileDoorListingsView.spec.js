/**
 * Field-capture screen. These tests exist because an adversarial review found
 * three real defects here, and each one is pinned below:
 *
 *  1. On total photo-upload failure the code cleared `shots` unconditionally —
 *     the blobs were gone, the row survived as pending_review with no photo, the
 *     office could not publish it, and the tech had nothing to retry with.
 *  2. The size chips are labelled in FEET ("16×7") directly above inputs
 *     labelled "(in)". Typing 16 and 7 produced a 1'4"x0'7" door that the
 *     backend accepts (ge=0) and the office then prices.
 *  3. feetInches() could emit 12 inches (83.5 -> 6'12").
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const api = { get: vi.fn(), post: vi.fn() }
vi.mock('../../composables/useApi', () => ({ useApi: () => api }))
const toastAdd = vi.fn()
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

import MobileDoorListingsView from '../MobileDoorListingsView.vue'

const stubs = {
  Toast: true,
}

function makeFile(name = 'door.jpg') {
  return new File([new Uint8Array([1, 2, 3])], name, { type: 'image/jpeg' })
}

async function mountView() {
  api.get.mockResolvedValue({ listings: [] })
  const w = mount(MobileDoorListingsView, { global: { stubs } })
  await flushPromises()
  return w
}

beforeEach(() => {
  vi.clearAllMocks()
  global.URL.createObjectURL = vi.fn(() => 'blob:preview')
  global.URL.revokeObjectURL = vi.fn()
})

describe('size entry — the feet/inches trap', () => {
  it('refuses feet typed into the inch fields, and says so', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    // A tech reads the "16×7" chip, then types 16 and 7 into "(in)".
    w.vm.form.width_in = 16
    w.vm.form.height_in = 7
    await flushPromises()

    expect(w.vm.canSubmit).toBe(false)
    expect(w.vm.blockedReason).toMatch(/INCHES, not feet/i)
    expect(w.vm.blockedReason).toContain('192')
  })

  it('accepts a real opening in inches', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.form.width_in = 192
    w.vm.form.height_in = 84
    await flushPromises()
    expect(w.vm.canSubmit).toBe(true)
    expect(w.vm.blockedReason).toBe('')
  })

  it('rejects an implausibly large opening', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.form.width_in = 4000
    w.vm.form.height_in = 84
    await flushPromises()
    expect(w.vm.canSubmit).toBe(false)
  })

  it('never renders 12 inches in the size hint', async () => {
    const w = await mountView()
    // 83.5 rounds to 84 -> exactly 7', NOT 6'12".
    w.vm.form.width_in = 192
    w.vm.form.height_in = 83.5
    await flushPromises()
    expect(w.vm.sizeHint).not.toMatch(/12"/)
    expect(w.vm.sizeHint).toContain("7'")
  })
})

describe('derived title', () => {
  it('reads as the office expects for a standard door', async () => {
    const w = await mountView()
    w.vm.form.width_in = 192
    w.vm.form.height_in = 84
    w.vm.form.color = 'Sandtone'
    await flushPromises()
    expect(w.vm.derivedTitle).toBe('16x7 Sandtone (from the field)')
  })

  it('keeps the odd inches instead of rounding a 16\'2" to 16', async () => {
    const w = await mountView()
    w.vm.form.width_in = 194
    w.vm.form.height_in = 84
    await flushPromises()
    expect(w.vm.derivedTitle).toContain('16\'2"')
  })
})

describe('submit — photo failure must not destroy the blobs', () => {
  it('keeps the failed shots and offers a photo-only retry', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.form.width_in = 192
    w.vm.form.height_in = 84
    await flushPromises()

    api.post.mockImplementation((url) => {
      if (url.endsWith('/photos')) return Promise.reject(new Error('offline'))
      return Promise.resolve({ id: 'row-1' })
    })
    await w.vm.submit()
    await flushPromises()

    // THE regression: the blob must still be here to retry with.
    expect(w.vm.shots).toHaveLength(1)
    expect(w.vm.pendingListingId).toBe('row-1')
    expect(w.vm.error).toMatch(/did not upload/i)
    expect(global.URL.revokeObjectURL).not.toHaveBeenCalled()
  })

  it('retry uploads against the existing row and then clears', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.pendingListingId = 'row-1'
    api.post.mockResolvedValue({ id: 'photo-1' })

    await w.vm.retryPhotos()
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith(
      '/api/door-listings/row-1/photos', expect.anything(), expect.anything(),
    )
    expect(w.vm.shots).toHaveLength(0)
    expect(w.vm.pendingListingId).toBeNull()
    expect(w.vm.error).toBeNull()
  })

  it('clears only on a fully successful submit', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.form.width_in = 192
    w.vm.form.height_in = 84
    await flushPromises()

    api.post.mockResolvedValue({ id: 'row-2' })
    await w.vm.submit()
    await flushPromises()

    expect(w.vm.shots).toHaveLength(0)
    expect(w.vm.form.width_in).toBeNull()
    expect(w.vm.error).toBeNull()
  })

  it('does not double-submit when tapped twice', async () => {
    const w = await mountView()
    w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.form.width_in = 192
    w.vm.form.height_in = 84
    await flushPromises()

    // Only the CREATE pends; photo uploads must still resolve or the test hangs
    // on the upload loop rather than exercising the re-entry guard.
    let resolveCreate
    api.post.mockImplementation((url) => (
      url.endsWith('/photos')
        ? Promise.resolve({ id: 'photo-x' })
        : new Promise((r) => { resolveCreate = r })
    ))
    const first = w.vm.submit()
    await w.vm.submit()            // second tap while the first is in flight
    resolveCreate({ id: 'row-3' })
    await first
    await flushPromises()

    const creates = api.post.mock.calls.filter(([u]) => u === '/api/door-listings')
    expect(creates).toHaveLength(1)
  })
})

describe('photo cap', () => {
  it('stops at 8 and tells the tech', async () => {
    const w = await mountView()
    for (let i = 0; i < 8; i += 1) w.vm.shots.push({ file: makeFile(), preview: 'blob:x' })
    w.vm.onPick({ target: { files: [makeFile('ninth.jpg')], value: '' } })
    await flushPromises()
    expect(w.vm.shots).toHaveLength(8)
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn' }))
  })
})
