/**
 * SS-31 slice G — FederationProviders.vue tenant-admin UI tests.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import FederationProviders from '../FederationProviders.vue'

function mkProvider(overrides = {}) {
  return {
    id: 'p1',
    tenant_id: 't1',
    kind: 'oidc',
    display_name: 'Okta',
    metadata_url: 'https://okta.example.com/.well-known/openid-configuration',
    client_id: 'c',
    has_client_secret: true,
    redirect_uri: 'https://sp/cb',
    sp_entity_id: null,
    acs_url: null,
    created_at: 1234,
    ...overrides,
  }
}

describe('FederationProviders.vue', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('lists registered providers on mount', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ items: [mkProvider()], total: 1 })
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    expect(fetchFn).toHaveBeenCalledWith('/api/federation/providers', { method: 'GET' })
    expect(wrapper.find('[data-testid="providers-list"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-p1"]').text()).toContain('Okta')
  })

  it('shows empty state when no providers', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ items: [], total: 0 })
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    expect(wrapper.find('[data-testid="empty"]').exists()).toBe(true)
  })

  it('disables submit until display_name + https metadata_url are provided', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ items: [], total: 0 })
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    const btn = wrapper.find('[data-testid="submit"]')
    expect(btn.attributes('disabled')).toBeDefined()
    await wrapper.find('[data-testid="display-name"]').setValue('Okta')
    await wrapper.find('[data-testid="metadata-url"]').setValue('http://insecure/x')
    expect(wrapper.find('[data-testid="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.find('[data-testid="metadata-url"]').setValue('https://okta/x')
    expect(wrapper.find('[data-testid="submit"]').attributes('disabled')).toBeUndefined()
  })

  it('posts OIDC provider and adds it to the list', async () => {
    const fetchFn = vi.fn()
      .mockResolvedValueOnce({ items: [], total: 0 })  // initial load
      .mockResolvedValueOnce(mkProvider({ id: 'p2', display_name: 'Azure AD' }))  // POST
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    await wrapper.find('[data-testid="display-name"]').setValue('Azure AD')
    await wrapper.find('[data-testid="metadata-url"]').setValue('https://login.microsoftonline.com/x')
    await wrapper.find('[data-testid="client-id"]').setValue('cid')
    await wrapper.find('[data-testid="client-secret"]').setValue('s3cr3t')
    await wrapper.find('[data-testid="register-form"]').trigger('submit')
    await flushPromises()

    const postCall = fetchFn.mock.calls[1]
    expect(postCall[0]).toBe('/api/federation/providers')
    expect(postCall[1].method).toBe('POST')
    const body = JSON.parse(postCall[1].body)
    expect(body.kind).toBe('oidc')
    expect(body.client_secret).toBe('s3cr3t')
    // Secret never persists in DOM after submit
    expect(wrapper.find('[data-testid="client-secret"]').element.value).toBe('')
    expect(wrapper.find('[data-testid="provider-p2"]').exists()).toBe(true)
  })

  it('hides OIDC fields and shows SAML fields when kind is saml', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ items: [], total: 0 })
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    await wrapper.find('[data-testid="kind"]').setValue('saml')
    expect(wrapper.find('[data-testid="client-id"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="sp-entity-id"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="acs-url"]').exists()).toBe(true)
  })

  it('requires explicit confirm before DELETE', async () => {
    const fetchFn = vi.fn()
      .mockResolvedValueOnce({ items: [mkProvider()], total: 1 })  // load
      .mockResolvedValueOnce(null)  // delete
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    await wrapper.find('[data-testid="delete-p1"]').trigger('click')
    // confirmation dialog present, DELETE not yet called
    expect(wrapper.find('[data-testid="delete-confirm"]').exists()).toBe(true)
    expect(fetchFn.mock.calls.length).toBe(1)
    await wrapper.find('[data-testid="delete-confirm-yes"]').trigger('click')
    await flushPromises()
    const delCall = fetchFn.mock.calls[1]
    expect(delCall[0]).toBe('/api/federation/providers/p1')
    expect(delCall[1].method).toBe('DELETE')
    expect(wrapper.find('[data-testid="provider-p1"]').exists()).toBe(false)
  })

  it('cancels delete without calling fetch', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ items: [mkProvider()], total: 1 })
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    await wrapper.find('[data-testid="delete-p1"]').trigger('click')
    await wrapper.find('[data-testid="delete-confirm-no"]').trigger('click')
    expect(wrapper.find('[data-testid="delete-confirm"]').exists()).toBe(false)
    expect(fetchFn.mock.calls.length).toBe(1)
  })

  it('surfaces backend error on list failure', async () => {
    const fetchFn = vi.fn().mockRejectedValue(new Error('boom'))
    const wrapper = mount(FederationProviders, { props: { fetchFn } })
    await flushPromises()
    expect(wrapper.find('[data-testid="error"]').text()).toContain('boom')
  })

  it('surfaces backend error on register failure', async () => {
    const fetchFn = vi.fn()
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockRejectedValueOnce(Object.assign(new Error('metadata_url_must_be_https'), {}))
    const wrapper = mount(FederationProviders, {
      props: { fetchFn, initialProviders: [] },
    })
    await flushPromises()
    await wrapper.find('[data-testid="display-name"]').setValue('X')
    await wrapper.find('[data-testid="metadata-url"]').setValue('https://x/y')
    await wrapper.find('[data-testid="register-form"]').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="error"]').text()).toContain('metadata_url_must_be_https')
  })
})
