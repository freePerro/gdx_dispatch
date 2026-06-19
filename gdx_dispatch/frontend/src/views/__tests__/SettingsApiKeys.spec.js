/**
 * Tests for SettingsApiKeys.vue (SS-14 slice F).
 *
 * Uses @vue/test-utils + a stubbed global fetch to exercise the three
 * user-facing flows: list load, mint-and-show-secret, revoke.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import SettingsApiKeys from '../SettingsApiKeys.vue'

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => bodyObj,
  }
}

// AppLayout pulls in AppSidebar (Pinia store) — stub so these tests stay
// focused on SettingsApiKeys' own behavior.
const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
}
const mountOpts = { global: { stubs } }

describe('SettingsApiKeys.vue', () => {
  let fetchMock

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    global.fetch = fetchMock
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('loads capabilities and existing tokens on mount', async () => {
    fetchMock.mockImplementation((url) => {
      if (url === '/api/capabilities/available') {
        return Promise.resolve(
          mkResponse([
            { id: 'cap-1', action: 'read', resource_type: 'jobs', label: 'Read jobs' },
          ]),
        )
      }
      if (url === '/api/pats') {
        return Promise.resolve(
          mkResponse([
            {
              id: 'pat-1',
              name: 'existing',
              prefix: 'gdx_pat_live_',
              created_at: '2026-01-01T00:00:00Z',
              expires_at: '2026-06-01T00:00:00Z',
              last_used_at: null,
            },
          ]),
        )
      }
      return Promise.reject(new Error(`unexpected url ${url}`))
    })

    const wrapper = mount(SettingsApiKeys, mountOpts)
    await flushPromises()

    expect(wrapper.find('[data-testid="pat-row-pat-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cap-row-cap-1"]').exists()).toBe(true)
  })

  it('disables create button until name is provided', async () => {
    fetchMock.mockResolvedValue(mkResponse([]))
    const wrapper = mount(SettingsApiKeys, mountOpts)
    await flushPromises()

    const btn = wrapper.get('[data-testid="pat-create-button"]')
    expect(btn.attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="pat-name-input"]').setValue('my-token')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('shows secret banner exactly once after a successful mint', async () => {
    const listResponses = [
      [], // initial load
      [
        {
          id: 'pat-new',
          name: 'my-token',
          prefix: 'gdx_pat_live_',
          created_at: '2026-01-01T00:00:00Z',
          expires_at: '2026-03-01T00:00:00Z',
          last_used_at: null,
        },
      ],
    ]
    let listIdx = 0

    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') {
        return Promise.resolve(mkResponse([]))
      }
      if (url === '/api/pats' && (!opts.method || opts.method === 'GET')) {
        return Promise.resolve(mkResponse(listResponses[listIdx++] ?? []))
      }
      if (url === '/api/pats' && opts.method === 'POST') {
        return Promise.resolve(
          mkResponse({
            id: 'pat-new',
            name: 'my-token',
            secret: 'gdx_pat_live_SECRETVALUE',
            prefix: 'gdx_pat_live_',
            expires_at: '2026-03-01T00:00:00Z',
            created_at: '2026-01-01T00:00:00Z',
          }),
        )
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(SettingsApiKeys, mountOpts)
    await flushPromises()

    await wrapper.get('[data-testid="pat-name-input"]').setValue('my-token')
    await wrapper.get('[data-testid="pat-create-button"]').trigger('click')
    await flushPromises()

    const banner = wrapper.get('[data-testid="pat-secret-banner"]')
    expect(banner.exists()).toBe(true)
    expect(wrapper.get('[data-testid="pat-secret-value"]').text()).toBe(
      'gdx_pat_live_SECRETVALUE',
    )
    // Tokens list refreshed.
    expect(wrapper.find('[data-testid="pat-row-pat-new"]').exists()).toBe(true)
  })

  it('displays backend error detail on mint failure', async () => {
    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/pats' && (!opts.method || opts.method === 'GET'))
        return Promise.resolve(mkResponse([]))
      if (url === '/api/pats' && opts.method === 'POST') {
        return Promise.resolve(
          mkResponse({ detail: 'cannot grant capability you don’t hold: write:invoice' }, { ok: false, status: 403 }),
        )
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(SettingsApiKeys, mountOpts)
    await flushPromises()
    await wrapper.get('[data-testid="pat-name-input"]').setValue('bad')
    await wrapper.get('[data-testid="pat-create-button"]').trigger('click')
    await flushPromises()

    const err = wrapper.get('[data-testid="pat-error"]')
    expect(err.text()).toContain("cannot grant capability")
    expect(wrapper.find('[data-testid="pat-secret-banner"]').exists()).toBe(false)
  })

  it('revoke hits DELETE /api/pats/:id and refreshes list', async () => {
    const deleteCalls = []
    let listResp = [
      {
        id: 'pat-1',
        name: 't1',
        prefix: 'gdx_pat_live_',
        created_at: '2026-01-01T00:00:00Z',
        expires_at: null,
        last_used_at: null,
      },
    ]

    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/pats' && (!opts.method || opts.method === 'GET')) {
        return Promise.resolve(mkResponse(listResp))
      }
      if (url.startsWith('/api/pats/') && opts.method === 'DELETE') {
        deleteCalls.push(url)
        listResp = []  // backend now reports no active tokens
        return Promise.resolve(mkResponse({ revoked: true, id: 'pat-1' }))
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(SettingsApiKeys, mountOpts)
    await flushPromises()
    await wrapper.get('[data-testid="pat-revoke-pat-1"]').trigger('click')
    await flushPromises()

    expect(deleteCalls).toEqual(['/api/pats/pat-1'])
    expect(wrapper.find('[data-testid="pat-row-pat-1"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pat-list-empty"]').exists()).toBe(true)
  })
})
