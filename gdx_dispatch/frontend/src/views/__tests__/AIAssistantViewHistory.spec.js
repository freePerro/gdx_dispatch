/**
 * Sprint 1.x-S35 — AIAssistantView.vue query history.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import AIAssistantView from '../AIAssistantView.vue'

const STUBS = { AppLayout: { template: '<div><slot /></div>' } }

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => bodyObj }
}

const ANSWER = (text) => mkResponse({
  answer: text,
  tools_used: [],
  disabled: false,
  reason: null,
  pending_action: null,
  result: null,
})

describe('AIAssistantView.vue — query history (S35)', () => {
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

  async function ask(wrapper, q) {
    await wrapper.get('[data-test="ai-question-input"]').setValue(q)
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
  }

  it('history is empty before any submit', async () => {
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-test="ai-history"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="ai-history-item"]').length).toBe(0)
  })

  it('appends each Q/A pair to history after a successful submit', async () => {
    const answers = ['First!', 'Second!', 'Third!']
    let i = 0
    fetchMock.mockImplementation(() => Promise.resolve(ANSWER(answers[i++])))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper, 'q1')
    await ask(wrapper, 'q2')
    await ask(wrapper, 'q3')
    const items = wrapper.findAll('[data-test="ai-history-item"]')
    expect(items.length).toBe(3)
    expect(items[0].html()).toContain('q3')
    expect(items[0].html()).toContain('Third!')
    expect(items[2].html()).toContain('q1')
    expect(items[2].html()).toContain('First!')
  })

  it('caps history at 50 entries', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(ANSWER('ok')))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    for (let i = 0; i < 55; i++) {
      await ask(wrapper, `question_${i}`)
    }
    const items = wrapper.findAll('[data-test="ai-history-item"]')
    expect(items.length).toBe(50)
    expect(items[0].html()).toContain('question_54')
    expect(items[49].html()).toContain('question_5')
  })

  it('does NOT add pending-only responses to history', async () => {
    let callCount = 0
    fetchMock.mockImplementation(() => {
      callCount++
      if (callCount === 1) {
        return Promise.resolve(mkResponse({
          answer: null,
          tools_used: ['invoices.create_draft'],
          disabled: false,
          reason: null,
          pending_action: {
            tool: 'invoices.create_draft',
            payload: { customer_id: 1 },
            approval_token: 'tok_abc',
            trace_id: 't1',
            diff: { line_items: [{ description: 'X', amount: 100 }] },
          },
          result: null,
        }))
      }
      return Promise.resolve(mkResponse({
        answer: 'Applied invoices.create_draft.',
        tools_used: ['invoices.create_draft'],
        disabled: false,
        reason: null,
        pending_action: null,
        result: { invoice_id: 99 },
      }))
    })
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper, 'create draft')
    expect(wrapper.findAll('[data-test="ai-history-item"]').length).toBe(0)
    await wrapper.get('[data-test="ai-yellow-apply"]').trigger('click')
    await flushPromises()
    const items = wrapper.findAll('[data-test="ai-history-item"]')
    expect(items.length).toBe(1)
    expect(items[0].html()).toContain('create draft')
    expect(items[0].html()).toContain('Applied invoices.create_draft.')
  })
})
