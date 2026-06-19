/**
 * Sprint 1.x-S34 — AIAssistantView.vue Red confirm modal.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import AIAssistantView from '../AIAssistantView.vue'

const STUBS = { AppLayout: { template: '<div><slot /></div>' } }

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => bodyObj }
}

const RED_PENDING = () => mkResponse({
  answer: null,
  tools_used: ['invoices.void'],
  disabled: false,
  reason: null,
  pending_action: {
    tool: 'invoices.void',
    payload: { invoice_id: 42 },
    approval_token: 'tok_red_xyz',
    trace_id: 'trace_void',
    diff: { invoice_number: 'INV-2026-0042', amount: 1500.00, status: 'will_void' },
  },
  result: null,
})

describe('AIAssistantView.vue — Red confirm modal (S34)', () => {
  let fetchMock

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    global.fetch = fetchMock
    sessionStorage.setItem('gdx_tenant_slug', 'gdx')
  })

  afterEach(() => {
    vi.restoreAllMocks()
    sessionStorage.clear()
  })

  async function ask(wrapper, q = 'void invoice INV-2026-0042') {
    await wrapper.get('[data-test="ai-question-input"]').setValue(q)
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
  }

  it('renders the Red modal when pending_action tool is invoices.void', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(RED_PENDING()))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    expect(wrapper.find('[data-test="ai-red-modal"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="ai-red-confirm-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="ai-red-apply"]').exists()).toBe(true)
    expect(wrapper.html()).toContain('INV-2026-0042')
  })

  it('Apply is disabled until the user types the invoice_number exactly', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(RED_PENDING()))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    const apply = wrapper.get('[data-test="ai-red-apply"]')
    expect(apply.attributes('disabled') !== undefined).toBe(true)
    await wrapper.get('[data-test="ai-red-confirm-input"]').setValue('wrong-id')
    expect(apply.attributes('disabled') !== undefined).toBe(true)
    await wrapper.get('[data-test="ai-red-confirm-input"]').setValue('INV-2026-0042')
    expect(apply.attributes('disabled')).toBeUndefined()
  })

  it('Apply with correct confirmation re-POSTs /api/ai/ask with approval_ref', async () => {
    let applyBody = null
    fetchMock.mockImplementation((url, opts) => {
      if (url === '/api/ai/ask' && opts?.method === 'POST') {
        const body = JSON.parse(opts.body)
        if (body.approval_ref) {
          applyBody = body
          return Promise.resolve(mkResponse({
            answer: 'Applied invoices.void.',
            tools_used: ['invoices.void'],
            disabled: false,
            reason: null,
            pending_action: null,
            result: { invoice_id: 42, status: 'voided' },
          }))
        }
        return Promise.resolve(RED_PENDING())
      }
      return Promise.reject(new Error(`unexpected url ${url} method ${opts?.method}`))
    })
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    await wrapper.get('[data-test="ai-red-confirm-input"]').setValue('INV-2026-0042')
    await wrapper.get('[data-test="ai-red-apply"]').trigger('click')
    await flushPromises()
    expect(applyBody).not.toBeNull()
    expect(applyBody.approval_ref).toBe('tok_red_xyz')
    expect(applyBody.tool).toBe('invoices.void')
    expect(wrapper.find('[data-test="ai-red-modal"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="ai-answer"]').html()).toContain('Applied invoices.void.')
  })

  it('Cancel clears the modal without firing a network call', async () => {
    let postCount = 0
    fetchMock.mockImplementation((url, opts) => {
      if (url === '/api/ai/ask' && opts?.method === 'POST') {
        postCount++
        return Promise.resolve(RED_PENDING())
      }
      return Promise.reject(new Error(`unexpected url ${url} method ${opts?.method}`))
    })
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    expect(postCount).toBe(1)
    await wrapper.get('[data-test="ai-red-cancel"]').trigger('click')
    await flushPromises()
    expect(postCount).toBe(1)
    expect(wrapper.find('[data-test="ai-red-modal"]').exists()).toBe(false)
  })
})
