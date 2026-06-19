/**
 * Sprint 1.x-S31 — AIAssistantView.vue text input + plain answer.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import AIAssistantView from '../AIAssistantView.vue'

const STUBS = { AppLayout: { template: '<div><slot /></div>' } }

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => bodyObj }
}

describe('AIAssistantView.vue (S31)', () => {
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

  it('renders an input + submit button on mount (no fetch yet)', async () => {
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-test="ai-question-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="ai-submit"]').exists()).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('POSTs to /api/ai/ask with the question and renders the answer', async () => {
    fetchMock.mockImplementation((url, opts) => {
      if (url === '/api/ai/ask' && opts?.method === 'POST') {
        const body = JSON.parse(opts.body)
        expect(body.question).toBe('What revenue this month?')
        return Promise.resolve(mkResponse({
          answer: 'Revenue this month is $12,345.',
          tools_used: ['revenue.summary'],
          disabled: false,
          reason: null,
          pending_action: null,
          result: null,
        }))
      }
      return Promise.reject(new Error(`unexpected url ${url} method ${opts?.method}`))
    })
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await wrapper.get('[data-test="ai-question-input"]').setValue('What revenue this month?')
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
    const answer = wrapper.get('[data-test="ai-answer"]')
    expect(answer.html()).toContain('Revenue this month is $12,345.')
  })

  it('shows the disabled message when response.disabled is true', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(mkResponse({
      answer: null,
      tools_used: [],
      disabled: true,
      reason: 'no_key',
      pending_action: null,
      result: null,
    })))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await wrapper.get('[data-test="ai-question-input"]').setValue('hello')
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.html()).toMatch(/disabled|configure.*key|no.*key/i)
  })

  it('disables Submit when the input is empty', async () => {
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await flushPromises()
    const button = wrapper.get('[data-test="ai-submit"]')
    expect(button.attributes('disabled') !== undefined).toBe(true)
    await wrapper.get('[data-test="ai-question-input"]').setValue('hi')
    expect(button.attributes('disabled')).toBeUndefined()
  })

  it('shows an error message on network failure', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(mkResponse({ detail: 'oops' }, { ok: false, status: 500 })))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await wrapper.get('[data-test="ai-question-input"]').setValue('hi')
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.html()).toMatch(/error|oops|failed/i)
  })

  it('shows a loading indicator while the request is in flight', async () => {
    let resolveFetch
    fetchMock.mockImplementation(() => new Promise((resolve) => { resolveFetch = resolve }))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await wrapper.get('[data-test="ai-question-input"]').setValue('hi')
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="ai-loading"]').exists()).toBe(true)
    resolveFetch(mkResponse({ answer: 'done', tools_used: [], disabled: false, reason: null, pending_action: null, result: null }))
    await flushPromises()
    expect(wrapper.find('[data-test="ai-loading"]').exists()).toBe(false)
  })
})
