import { describe, it, expect, vi, afterEach } from 'vitest'
import { Capacitor } from '@capacitor/core'
import { App } from '@capacitor/app'

vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: vi.fn(() => false) },
  registerPlugin: vi.fn(() => ({})),
}))
vi.mock('@capacitor/app', () => ({ App: { getInfo: vi.fn() } }))

import { _setAppMode, detectAppMode, isPlannerApp, plannerAppRoute, PLANNER_APP_PATH } from '../appMode'

describe('appMode', () => {
  afterEach(() => {
    _setAppMode('dispatch')
    vi.clearAllMocks()
  })

  it('a browser is GDX Dispatch, without asking for the app id', async () => {
    Capacitor.isNativePlatform.mockReturnValue(false)
    expect(await detectAppMode()).toBe('dispatch')
    expect(App.getInfo).not.toHaveBeenCalled()
  })

  it('the com.gdx.planner app is GDX Planner', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    App.getInfo.mockResolvedValue({ id: 'com.gdx.planner' })
    expect(await detectAppMode()).toBe('planner')
    expect(isPlannerApp()).toBe(true)
  })

  it('the com.gdx.dispatch app is GDX Dispatch', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    App.getInfo.mockResolvedValue({ id: 'com.gdx.dispatch' })
    expect(await detectAppMode()).toBe('dispatch')
  })

  it('an unreadable app id runs as GDX Dispatch rather than failing the boot', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    App.getInfo.mockRejectedValue(new Error('no bridge'))
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(await detectAppMode()).toBe('dispatch')
  })

  describe('plannerAppRoute', () => {
    it('lets the planner page, sign-in, reset and the password change through', () => {
      for (const path of [PLANNER_APP_PATH, '/login', '/forgot-password', '/reset-password', '/profile']) {
        expect(plannerAppRoute({ path, query: {} })).toBe(true)
      }
    })

    it('maps a planner link onto the planner page, keeping the query', () => {
      expect(plannerAppRoute({ path: '/mobile/planner', query: { tab: 'tasks' } }))
        .toEqual({ path: PLANNER_APP_PATH, query: { tab: 'tasks' } })
      expect(plannerAppRoute({ path: '/planner', query: { tab: 'plans' } }))
        .toEqual({ path: PLANNER_APP_PATH, query: { tab: 'plans' } })
    })

    it('sends anything else to the planner page, dropping its query', () => {
      expect(plannerAppRoute({ path: '/dashboard', query: {} })).toEqual({ path: PLANNER_APP_PATH })
      expect(plannerAppRoute({ path: '/jobs/abc', query: { tab: 'photos' } })).toEqual({ path: PLANNER_APP_PATH })
      expect(plannerAppRoute({ path: '/mobile', query: {} })).toEqual({ path: PLANNER_APP_PATH })
    })
  })
})
