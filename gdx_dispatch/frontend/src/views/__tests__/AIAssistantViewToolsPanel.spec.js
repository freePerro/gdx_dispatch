/**
 * Sprint 1.x-S32 — AIAssistantView.vue tools-used panel.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import AIAssistantView from '../AIAssistantView.vue'

const STUBS = { AppLayout: { template: '<div><slot /></div>' } }

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => bodyObj }
}

describe('AIAssistantView.vue — tools-used panel (S32)', () => {
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

  async function ask(wrapper, q = 'hello') {
    await wrapper.get('[data-test="ai-question-input"]').setValue(q)
    await wrapper.get('[data-test="ai-submit"]').trigger('click')
    await flushPromises()
  }

  it('shows the tools-used panel when tools_used is non-empty', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(mkResponse({
      answer: 'Result: $12,345',
      tools_used: ['revenue.summary', 'customers.lifetime_analysis'],
      disabled: false,
      reason: null,
      pending_action: null,
      result: null,
    })))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    const panel = wrapper.find('[data-test="ai-tools-used"]')
    expect(panel.exists()).toBe(true)
    // Default-collapsed: tool names are in the toggle counter, not the list.
    expect(panel.html()).toContain('(2)')
  })

  it('hides the tools-used panel when tools_used is empty', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(mkResponse({
      answer: 'Hi!',
      tools_used: [],
      disabled: false,
      reason: null,
      pending_action: null,
      result: null,
    })))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    expect(wrapper.find('[data-test="ai-tools-used"]').exists()).toBe(false)
  })

  it('hides the tools-used panel before the first response', async () => {
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-test="ai-tools-used"]').exists()).toBe(false)
  })

  it('toggle expand/collapse changes the visible tool list', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(mkResponse({
      answer: 'ok',
      tools_used: ['jobs.list', 'invoices.list'],
      disabled: false,
      reason: null,
      pending_action: null,
      result: null,
    })))
    const wrapper = mount(AIAssistantView, { global: { stubs: STUBS } })
    await ask(wrapper)
    const toggle = wrapper.get('[data-test="ai-tools-used-toggle"]')
    expect(wrapper.find('[data-test="ai-tools-used-list"]').exists()).toBe(false)
    await toggle.trigger('click')
    await flushPromises()
    const list = wrapper.get('[data-test="ai-tools-used-list"]')
    expect(list.html()).toContain('jobs.list')
    expect(list.html()).toContain('invoices.list')
    await toggle.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="ai-tools-used-list"]').exists()).toBe(false)
  })
})
