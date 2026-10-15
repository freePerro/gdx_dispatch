// A session that ended without the person choosing it (router/index.js,
// useApi) lands on /login?reason=expired: the page says why they are there,
// and a plain visit to /login says nothing (2026-10-15).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const routeState = { query: {} }
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(() => Promise.resolve()) }),
  useRoute: () => routeState,
}))
const authState = { user: null, login: vi.fn() }
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }))
vi.mock('../../stores/theme', () => ({
  useThemeStore: () => ({ loadBranding: vi.fn(() => Promise.resolve()) }),
}))

import LoginView from '../LoginView.vue'

describe('LoginView session-expired notice', () => {
  beforeEach(() => { routeState.query = {} })

  it('says the session expired when sent here by one', async () => {
    routeState.query = { redirect: '/dispatch', reason: 'expired' }
    const wrapper = mount(LoginView)
    await flushPromises()
    expect(wrapper.get('[data-testid="login-session-expired"]').text()).toContain('Your session expired')
  })

  it('says nothing on a plain visit', async () => {
    routeState.query = { redirect: '/dispatch' }
    const wrapper = mount(LoginView)
    await flushPromises()
    expect(wrapper.find('[data-testid="login-session-expired"]').exists()).toBe(false)
  })

  it('gives way to a login error', async () => {
    routeState.query = { reason: 'expired' }
    authState.login = vi.fn(async () => { throw new Error('Invalid email or password') })
    const wrapper = mount(LoginView)
    await wrapper.get('[data-testid="login-email"]').setValue('owner@example.com')
    await wrapper.get('[data-testid="login-password"]').setValue('wrong')
    await wrapper.get('[data-testid="login-form"]').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="login-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="login-session-expired"]').exists()).toBe(false)
  })
})
