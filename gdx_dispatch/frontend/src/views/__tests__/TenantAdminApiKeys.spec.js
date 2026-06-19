/**
 * Tests for TenantAdminApiKeys.vue (SS-15 slice D).
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import TenantAdminApiKeys from '../TenantAdminApiKeys.vue'

function mkResponse(bodyObj, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => bodyObj,
  }
}

// AppLayout pulls in AppSidebar (Pinia store) — stub.
const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
}
const mountOpts = { global: { stubs } }

describe('TenantAdminApiKeys.vue', () => {
  let fetchMock

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    global.fetch = fetchMock
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('loads members, capabilities, and existing admin-issued tokens on mount', async () => {
    fetchMock.mockImplementation((url) => {
      if (url === '/api/capabilities/available') {
        return Promise.resolve(
          mkResponse([{ id: 'cap-1', action: 'read', resource_type: 'jobs', label: 'Read jobs' }]),
        )
      }
      if (url === '/api/admin/tenant-members') {
        return Promise.resolve(
          mkResponse([
            { identity_id: 'id-1', email: 'tech@example.com', display_name: 'Tech A', role: 'tech' },
          ]),
        )
      }
      if (url === '/api/admin/pats') {
        return Promise.resolve(
          mkResponse([
            {
              id: 'pat-1',
              name: 'existing',
              owner_identity_id: 'id-1',
              status: 'active',
              prefix: 'gdx_pat_live_',
              created_at: '2026-01-01T00:00:00Z',
              expires_at: '2026-06-01T00:00:00Z',
            },
          ]),
        )
      }
      return Promise.reject(new Error(`unexpected ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()

    expect(wrapper.find('[data-testid="admin-pat-row-pat-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-cap-row-cap-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-target-select"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-target-opt-id-1"]').exists()).toBe(true)
  })

  it('shows free-form UUID input when members endpoint fails', async () => {
    fetchMock.mockImplementation((url) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members')
        return Promise.resolve(mkResponse({}, { ok: false, status: 404 }))
      if (url === '/api/admin/pats') return Promise.resolve(mkResponse([]))
      return Promise.reject(new Error(`unexpected ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()

    expect(wrapper.find('[data-testid="admin-target-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-target-select"]').exists()).toBe(false)
  })

  it('disables issue button until name and target are provided', async () => {
    fetchMock.mockImplementation((url) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members')
        return Promise.resolve(mkResponse({}, { ok: false, status: 404 }))
      if (url === '/api/admin/pats') return Promise.resolve(mkResponse([]))
      return Promise.reject(new Error(`unexpected ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()

    const btn = wrapper.get('[data-testid="admin-pat-create-button"]')
    expect(btn.attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="admin-target-input"]').setValue('00000000-0000-0000-0000-000000000001')
    await wrapper.get('[data-testid="admin-pat-name-input"]').setValue('my-token')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('shows secret banner when issued token is active', async () => {
    const listResponses = [
      [],
      [
        {
          id: 'pat-new',
          name: 't',
          owner_identity_id: 'id-1',
          status: 'active',
          prefix: 'gdx_pat_live_',
          created_at: '2026-01-01',
          expires_at: '2026-03-01',
        },
      ],
    ]
    let listIdx = 0

    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && (!opts.method || opts.method === 'GET')) {
        return Promise.resolve(mkResponse(listResponses[listIdx++] ?? []))
      }
      if (url === '/api/admin/pats' && opts.method === 'POST') {
        return Promise.resolve(
          mkResponse({
            id: 'pat-new',
            name: 't',
            target_identity_id: 'id-1',
            status: 'active',
            secret: 'gdx_pat_live_SECRET',
            prefix: 'gdx_pat_live_',
          }),
        )
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()
    await wrapper
      .get('[data-testid="admin-target-input"]')
      .setValue('00000000-0000-0000-0000-000000000001')
    await wrapper.get('[data-testid="admin-pat-name-input"]').setValue('t')
    await wrapper.get('[data-testid="admin-pat-create-button"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="admin-pat-secret-value"]').text()).toBe(
      'gdx_pat_live_SECRET',
    )
    expect(wrapper.find('[data-testid="admin-pat-row-pat-new"]').exists()).toBe(true)
  })

  it('shows pending-approval notice when issued token requires approval', async () => {
    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && (!opts.method || opts.method === 'GET'))
        return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && opts.method === 'POST') {
        return Promise.resolve(
          mkResponse({
            id: 'pat-pending',
            name: 'w',
            status: 'pending_approval',
            prefix: 'gdx_pat_live_',
          }),
        )
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()
    await wrapper
      .get('[data-testid="admin-target-input"]')
      .setValue('00000000-0000-0000-0000-000000000001')
    await wrapper.get('[data-testid="admin-pat-name-input"]').setValue('w')
    await wrapper.get('[data-testid="admin-pat-create-button"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="admin-pat-pending"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-pat-secret-wrap"]').exists()).toBe(false)
  })

  it('approve flow calls POST /approve and updates list', async () => {
    const approvedCalls = []
    let listResp = [
      {
        id: 'pat-1',
        name: 't',
        owner_identity_id: 'id-1',
        status: 'pending_approval',
        prefix: 'gdx_pat_live_',
        created_at: '2026-01-01',
        expires_at: '2026-03-01',
      },
    ]
    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && (!opts.method || opts.method === 'GET')) {
        return Promise.resolve(mkResponse(listResp))
      }
      if (url.endsWith('/approve') && opts.method === 'POST') {
        approvedCalls.push(url)
        listResp = [{ ...listResp[0], status: 'active' }]
        return Promise.resolve(mkResponse({ approved: true, id: 'pat-1', status: 'active', secret: 'gdx_pat_live_APPROVED' }))
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()
    await wrapper.get('[data-testid="admin-pat-approve-pat-1"]').trigger('click')
    await flushPromises()

    expect(approvedCalls).toEqual(['/api/admin/pats/pat-1/approve'])
    expect(wrapper.get('[data-testid="admin-pat-status-pat-1"]').text()).toBe('active')
    expect(wrapper.get('[data-testid="admin-pat-secret-value"]').text()).toBe(
      'gdx_pat_live_APPROVED',
    )
  })

  it('revoke hits DELETE and refreshes', async () => {
    const deleteCalls = []
    let listResp = [
      {
        id: 'pat-1',
        name: 't',
        owner_identity_id: 'id-1',
        status: 'active',
        prefix: 'gdx_pat_live_',
        created_at: '2026-01-01',
        expires_at: null,
      },
    ]
    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && (!opts.method || opts.method === 'GET'))
        return Promise.resolve(mkResponse(listResp))
      if (url.startsWith('/api/admin/pats/') && opts.method === 'DELETE') {
        deleteCalls.push(url)
        listResp = []
        return Promise.resolve(mkResponse({ revoked: true, id: 'pat-1' }))
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()
    await wrapper.get('[data-testid="admin-pat-revoke-pat-1"]').trigger('click')
    await flushPromises()

    expect(deleteCalls).toEqual(['/api/admin/pats/pat-1'])
    expect(wrapper.find('[data-testid="admin-pat-list-empty"]').exists()).toBe(true)
  })

  it('shows backend error on issue failure', async () => {
    fetchMock.mockImplementation((url, opts = {}) => {
      if (url === '/api/capabilities/available') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/tenant-members') return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && (!opts.method || opts.method === 'GET'))
        return Promise.resolve(mkResponse([]))
      if (url === '/api/admin/pats' && opts.method === 'POST') {
        return Promise.resolve(
          mkResponse({ detail: 'target user not in your tenant' }, { ok: false, status: 403 }),
        )
      }
      return Promise.reject(new Error(`unexpected ${opts.method} ${url}`))
    })

    const wrapper = mount(TenantAdminApiKeys, mountOpts)
    await flushPromises()
    await wrapper
      .get('[data-testid="admin-target-input"]')
      .setValue('00000000-0000-0000-0000-000000000099')
    await wrapper.get('[data-testid="admin-pat-name-input"]').setValue('bad')
    await wrapper.get('[data-testid="admin-pat-create-button"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="admin-pat-error"]').text()).toContain(
      'target user not in your tenant',
    )
  })
})
