/**
 * Sprint 1.x-S33 — AIAssistantView.vue Yellow confirm card.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import AIAssistantView from '../AIAssistantView.vue'

const STUBS = { AppLayout: { template: '<div><slot /></div>' } }

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => bodyObj }
}

const PENDING_RESPONSE = () => mkResponse({
  answer: null,
  tools_used: ['invoices.create_draft'],
  disabled: false,
  reason: null,
  pending_action: {
    tool: 'invoices.create_draft',
    payload: { customer_id: 42, amount: 250.00 },
    approval_token: 'tok_abc123',
    trace_id: 'trace_xyz',
    diff: { line_items: [{ description: 'Repair', amount: 250.00 }] },
  },
  result: null,
})

describe('AIAssistantView.vue — Yellow confirm card (S33)', () => {
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

  async function ask(wrapper, q = 'create a draft invoice') {
    await wrapper.get('[data-test="ai-question-input"]').setValue(q)
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
  }

  it('renders the Yellow confirm card when response has a pending_action', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(PENDING_RESPONSE()))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    expect(wrapper.find('[data-test="ai-yellow-confirm"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="ai-yellow-tool"]').html()).toContain('invoices.create_draft')
    expect(wrapper.get('[data-test="ai-yellow-apply"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="ai-yellow-cancel"]').exists()).toBe(true)
  })

  it('Apply re-POSTs /api/ai/ask with approval_ref + tool + payload', async () => {
    let applyCallBody = null
    fetchMock.mockImplementation((url, opts) => {
      if (url === '/api/ai/ask' && opts?.method === 'POST') {
        const body = JSON.parse(opts.body)
        if (body.approval_ref) {
          applyCallBody = body
          return Promise.resolve(mkResponse({
            answer: 'Applied invoices.create_draft.',
            tools_used: ['invoices.create_draft'],
            disabled: false,
            reason: null,
            pending_action: null,
            result: { invoice_id: 42, status: 'draft' },
          }))
        }
        return Promise.resolve(PENDING_RESPONSE())
      }
      return Promise.reject(new Error(`unexpected url ${url} method ${opts?.method}`))
    })
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    await wrapper.get('[data-test="ai-yellow-apply"]').trigger('click')
    await flushPromises()
    expect(applyCallBody).not.toBeNull()
    expect(applyCallBody.approval_ref).toBe('tok_abc123')
    expect(applyCallBody.tool).toBe('invoices.create_draft')
    expect(applyCallBody.payload).toEqual({ customer_id: 42, amount: 250.00 })
    expect(wrapper.find('[data-test="ai-yellow-confirm"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="ai-answer"]').html()).toContain('Applied invoices.create_draft.')
  })

  it('Cancel clears the pending card without firing a network call', async () => {
    let postCount = 0
    fetchMock.mockImplementation((url, opts) => {
      if (url === '/api/ai/ask' && opts?.method === 'POST') {
        postCount++
        return Promise.resolve(PENDING_RESPONSE())
      }
      return Promise.reject(new Error(`unexpected url ${url} method ${opts?.method}`))
    })
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    expect(postCount).toBe(1)
    await wrapper.get('[data-test="ai-yellow-cancel"]').trigger('click')
    await flushPromises()
    expect(postCount).toBe(1)
    expect(wrapper.find('[data-test="ai-yellow-confirm"]').exists()).toBe(false)
  })

  it('renders the diff JSON for inspection', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(PENDING_RESPONSE()))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    const diff = wrapper.get('[data-test="ai-yellow-diff"]')
    expect(diff.html()).toContain('Repair')
  })
})
