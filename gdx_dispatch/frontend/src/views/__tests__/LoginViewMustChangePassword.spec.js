// GDXA-1003: a login whose account must change its password lands on the
// change-password form (/profile), not the remembered target — including an
// /oauth/ target, which is a full-page load the router guard never sees.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const push = vi.fn(() => Promise.resolve())
const routeState = { query: {} }
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
  useRoute: () => routeState,
}))

const authState = { user: null, login: vi.fn() }
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }))
vi.mock('../../stores/theme', () => ({
  useThemeStore: () => ({ loadBranding: vi.fn(() => Promise.resolve()) }),
}))

import LoginView from '../LoginView.vue'

async function submit() {
  const wrapper = mount(LoginView)
  await wrapper.get('[data-testid="login-email"]').setValue('owner@example.com')
  await wrapper.get('[data-testid="login-password"]').setValue('printed-pw')
  await wrapper.get('[data-testid="login-form"]').trigger('submit')
  await flushPromises()
  return wrapper
}

describe('LoginView must_change_password', () => {
  let assign
  beforeEach(() => {
    push.mockClear()
    routeState.query = {}
    assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
  })

  function loginAs(flag) {
    authState.login = vi.fn(async () => {
      authState.user = { id: 'u1', must_change_password: flag }
    })
  }

  it('sends a flagged account to /profile', async () => {
    loginAs(true)
    routeState.query = { redirect: '/jobs' }
    await submit()
    expect(push).toHaveBeenCalledWith('/profile')
    expect(push).not.toHaveBeenCalledWith('/jobs')
  })

  it('does not take a flagged account through an /oauth/ full-page redirect', async () => {
    loginAs(true)
    routeState.query = { redirect: '/oauth/authorize?x=1' }
    await submit()
    expect(assign).not.toHaveBeenCalled()
    expect(push).toHaveBeenCalledWith('/profile')
  })

  it('leaves an unflagged account on its remembered target', async () => {
    loginAs(false)
    routeState.query = { redirect: '/jobs' }
    await submit()
    expect(push).toHaveBeenCalledWith('/jobs')
    expect(push).not.toHaveBeenCalledWith('/profile')
  })
})

// Audit 2026-10-14: the server gear is an Android-app setting. On the web it
// rerouted every API call, the login password included.
describe('LoginView server gear', () => {
  it('is not offered in the web app', () => {
    const wrapper = mount(LoginView)
    expect(wrapper.find('[data-testid="server-settings-gear"]').exists()).toBe(false)
  })
})
