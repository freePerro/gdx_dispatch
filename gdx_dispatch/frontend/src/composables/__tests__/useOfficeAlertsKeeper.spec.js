// /audit 2026-10-14: the owner check lived in the bottom nav (narrow screens
// only) and keyed on auth.user, which a cold-start session restore never sets.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { effectScope, nextTick, reactive } from 'vue'

const { sync, auth, perms } = vi.hoisted(() => ({
  sync: vi.fn(() => Promise.resolve()),
  auth: { claims: null, user: null },
  perms: { office: false },
}))

vi.mock('../../lib/officeAlerts', () => ({
  isOfficeAlertsAvailable: () => true,
  syncOfficeAlerts: sync,
}))
vi.mock('../useApi', () => ({ createApiClient: () => ({}) }))

let authState
let permState
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }))
vi.mock('../usePermission', () => ({
  usePermission: () => ({ hasPermission: (k) => k === 'nav.office' && permState.office }),
}))

import { useOfficeAlertsKeeper } from '../useOfficeAlertsKeeper'

const calls = () => sync.mock.calls.map(([, opts]) => opts)

describe('useOfficeAlertsKeeper', () => {
  let scope
  beforeEach(() => {
    sync.mockClear()
    authState = reactive({ ...auth })
    permState = reactive({ ...perms })
    scope?.stop()
    scope = effectScope()
  })

  it('takes the user from the access token when auth.user is empty (cold start)', () => {
    authState.claims = { sub: 'user-a' }
    scope.run(useOfficeAlertsKeeper)
    expect(calls()).toEqual([{ userId: 'user-a', office: false }])
  })

  it('does nothing while nobody is signed in', () => {
    scope.run(useOfficeAlertsKeeper)
    expect(sync).not.toHaveBeenCalled()
  })

  it('keeps an office user\'s alerts once, and checks the owner on every user change', async () => {
    authState.claims = { sub: 'user-a' }
    scope.run(useOfficeAlertsKeeper)
    permState.office = true
    await nextTick()
    permState.office = false
    await nextTick()
    permState.office = true
    await nextTick()
    authState.claims = { sub: 'user-b' }
    await nextTick()
    expect(calls()).toEqual([
      { userId: 'user-a', office: false },
      { userId: 'user-a', office: true },
      { userId: 'user-b', office: true },
    ])
  })

  it('a token refresh for the same user does not sync again', async () => {
    authState.claims = { sub: 'user-a', jti: '1' }
    scope.run(useOfficeAlertsKeeper)
    authState.claims = { sub: 'user-a', jti: '2' }
    await nextTick()
    authState.claims = { sub: 'user-a', jti: '3' }
    await nextTick()
    expect(calls()).toEqual([{ userId: 'user-a', office: false }])
  })
})
