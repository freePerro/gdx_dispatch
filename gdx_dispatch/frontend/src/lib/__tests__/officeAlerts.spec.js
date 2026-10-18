import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Capacitor } from '@capacitor/core'

const { plugin, listeners } = vi.hoisted(() => {
  const listeners = {}
  return {
    listeners,
    plugin: {
      start: vi.fn(),
      stop: vi.fn(),
      resume: vi.fn(),
      revokeToken: vi.fn(),
      status: vi.fn(),
      openBatterySettings: vi.fn(),
      addListener: vi.fn((name, fn) => { listeners[name] = fn }),
    },
  }
})

vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: vi.fn(), getPlatform: vi.fn() },
  registerPlugin: vi.fn(() => plugin),
}))
vi.mock('../serverConfig', () => ({ getServerUrl: vi.fn(() => 'https://dispatch.example.com') }))

import {
  MINT_TIMEOUT_MS,
  OWNER_KEY,
  ROTATE_AFTER_MS,
  checkOfficeAlertsOwner,
  disableOfficeAlerts,
  enableOfficeAlerts,
  ensureOfficeAlerts,
  getOfficeAlertsStatus,
  installOfficeAlertsBridge,
  isOfficeAlertsAvailable,
  syncOfficeAlerts,
} from '../officeAlerts'
import { getServerUrl } from '../serverConfig'

const OWNER = '0b6c3f1e-2d4a-4b8e-9c1d-7e5f6a7b8c9d'
const api = () => ({ post: vi.fn().mockResolvedValue({ token: 'tok-new', registration_id: 'r1', user_id: OWNER }) })
const mine = () => localStorage.setItem(OWNER_KEY, OWNER)
const native = (on = true) => {
  Capacitor.isNativePlatform.mockReturnValue(on)
  Capacitor.getPlatform.mockReturnValue(on ? 'android' : 'web')
}

describe('officeAlerts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    getServerUrl.mockReturnValue('https://dispatch.example.com')
  })

  it('is a no-op off native Android', async () => {
    native(false)
    const a = api()
    expect(isOfficeAlertsAvailable()).toBe(false)
    expect((await enableOfficeAlerts(a)).enabled).toBe(false)
    expect((await getOfficeAlertsStatus()).enabled).toBe(false)
    expect((await ensureOfficeAlerts(a)).enabled).toBe(false)
    await disableOfficeAlerts()
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.start).not.toHaveBeenCalled()
    expect(plugin.stop).not.toHaveBeenCalled()
  })

  it('enable mints a device token with the session and hands it to the service', async () => {
    native()
    plugin.start.mockResolvedValue({ enabled: true, running: true })
    const a = api()
    const res = await enableOfficeAlerts(a)
    expect(a.post).toHaveBeenCalledWith('/api/notifications/device-alerts/token',
      expect.objectContaining({ device_label: expect.any(String) }))
    // The installed package picks the feed, not the web layer (#1193).
    expect(plugin.start).toHaveBeenCalledWith(expect.objectContaining({ serverUrl: 'https://dispatch.example.com', token: 'tok-new' }))
    expect(res.enabled).toBe(true)
    expect(localStorage.getItem(OWNER_KEY)).toBe(OWNER)
  })

  it('enable refuses without a server address and mints nothing', async () => {
    native()
    getServerUrl.mockReturnValue('')
    const a = api()
    await expect(enableOfficeAlerts(a)).rejects.toThrow(/server address/)
    expect(a.post).not.toHaveBeenCalled()
  })

  it('disable stops the service and revokes', async () => {
    native()
    plugin.stop.mockResolvedValue({ enabled: false, revoked: true })
    await disableOfficeAlerts('logout')
    expect(plugin.stop).toHaveBeenCalledWith({ revoke: true, reason: 'logout' })
  })

  it('ensure never turns alerts on by itself', async () => {
    native()
    plugin.status.mockResolvedValue({ enabled: false })
    const a = api()
    await ensureOfficeAlerts(a, { userId: OWNER })
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.start).not.toHaveBeenCalled()
    expect(plugin.resume).not.toHaveBeenCalled()
  })

  // /audit 2026-10-14: after a cold start auth.user was null, and an ensure
  // with no user resumed and re-minted the last owner's alerts.
  it('ensure without a signed-in user neither resumes nor mints', async () => {
    native()
    mine()
    plugin.status.mockResolvedValue({ enabled: true, running: false, serverUrl: 'https://dispatch.example.com', tokenSavedAt: 0, notificationsAllowed: true })
    const a = api()
    const res = await ensureOfficeAlerts(a, {})
    expect(res.enabled).toBe(true)
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.resume).not.toHaveBeenCalled()
    expect(plugin.stop).not.toHaveBeenCalled()
  })

  it('ensure re-mints a token older than a week', async () => {
    native()
    const now = 10 * ROTATE_AFTER_MS
    plugin.status.mockResolvedValue({ enabled: true, running: true, serverUrl: 'https://dispatch.example.com', tokenSavedAt: now - ROTATE_AFTER_MS - 1, notificationsAllowed: true })
    plugin.start.mockResolvedValue({ enabled: true, running: true })
    mine()
    const a = api()
    await ensureOfficeAlerts(a, { now, userId: OWNER })
    expect(a.post).toHaveBeenCalledTimes(1)
    expect(plugin.start).toHaveBeenCalledWith(expect.objectContaining({ serverUrl: 'https://dispatch.example.com', token: 'tok-new' }))
  })

  // /audit 2026-10-14: with notifications blocked every launch minted a
  // token, prompted, was refused and left the token live for 90 days.
  it('ensure does not re-mint while notifications are blocked', async () => {
    native()
    mine()
    const now = 10 * ROTATE_AFTER_MS
    plugin.status.mockResolvedValue({ enabled: true, running: true, serverUrl: 'https://dispatch.example.com', tokenSavedAt: 0, notificationsAllowed: false })
    const a = api()
    await ensureOfficeAlerts(a, { now, userId: OWNER })
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.start).not.toHaveBeenCalled()
  })

  it('a mint that never answers gives up and frees the queue', async () => {
    vi.useFakeTimers()
    try {
      native()
      plugin.stop.mockResolvedValue({ enabled: false })
      const a = { post: vi.fn(() => new Promise(() => {})) }
      const enabling = enableOfficeAlerts(a)
      const disabling = disableOfficeAlerts('logout')
      const failed = expect(enabling).rejects.toThrow(/did not answer/)
      await vi.advanceTimersByTimeAsync(MINT_TIMEOUT_MS)
      await failed
      await disabling
      expect(plugin.start).not.toHaveBeenCalled()
      expect(plugin.stop).toHaveBeenCalledWith({ revoke: true, reason: 'logout' })
    } finally {
      vi.useRealTimers()
    }
  })

  it('a mint that answers after giving up is revoked, never started', async () => {
    vi.useFakeTimers()
    try {
      native()
      let answer
      const a = { post: vi.fn(() => new Promise((r) => { answer = r })) }
      const enabling = enableOfficeAlerts(a)
      const failed = expect(enabling).rejects.toThrow(/did not answer/)
      await vi.advanceTimersByTimeAsync(MINT_TIMEOUT_MS)
      await failed
      answer({ token: 'tok-late', registration_id: 'r9', user_id: OWNER })
      await vi.advanceTimersByTimeAsync(1)
      expect(plugin.revokeToken).toHaveBeenCalledWith({ serverUrl: 'https://dispatch.example.com', token: 'tok-late' })
      expect(plugin.start).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('ensure resumes a stopped service with a fresh token, without minting', async () => {
    native()
    const now = 10 * ROTATE_AFTER_MS
    plugin.status.mockResolvedValue({ enabled: true, running: false, serverUrl: 'https://dispatch.example.com', tokenSavedAt: now - 1000 })
    plugin.resume.mockResolvedValue({ enabled: true, running: true })
    mine()
    const a = api()
    const res = await ensureOfficeAlerts(a, { now, userId: OWNER })
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.resume).toHaveBeenCalled()
    expect(res.running).toBe(true)
  })

  it('ensure turns alerts off when the app now points at another server', async () => {
    native()
    plugin.status.mockResolvedValue({ enabled: true, running: true, serverUrl: 'https://old.example.com', tokenSavedAt: Date.now() })
    plugin.stop.mockResolvedValue({ enabled: false })
    mine()
    const a = api()
    await ensureOfficeAlerts(a, { userId: OWNER })
    expect(plugin.stop).toHaveBeenCalled()
    expect(a.post).not.toHaveBeenCalled()
  })

  // /audit 2026-10-14: an automatic logout leaves alerts on, so the next
  // person to sign in on this phone must not inherit them.
  it('someone else signing in turns the alerts off and revokes them', async () => {
    native()
    localStorage.setItem(OWNER_KEY, OWNER)
    plugin.status.mockResolvedValue({ enabled: true, running: true })
    plugin.stop.mockResolvedValue({ enabled: false })
    await checkOfficeAlertsOwner('5a5a5a5a-0000-4000-8000-000000000001')
    expect(plugin.stop).toHaveBeenCalledWith({ revoke: true, reason: 'user_changed' })
    expect(localStorage.getItem(OWNER_KEY)).toBeNull()
  })

  it('the owner signing back in keeps them, however the id is written', async () => {
    native()
    localStorage.setItem(OWNER_KEY, OWNER)
    plugin.status.mockResolvedValue({ enabled: true, running: true })
    await checkOfficeAlertsOwner(OWNER.replace(/-/g, '').toUpperCase())
    expect(plugin.stop).not.toHaveBeenCalled()
  })

  it('alerts with no recorded owner count as someone else\'s', async () => {
    native()
    plugin.status.mockResolvedValue({ enabled: true, running: true })
    plugin.stop.mockResolvedValue({ enabled: false })
    await checkOfficeAlertsOwner(OWNER)
    expect(plugin.stop).toHaveBeenCalledWith({ revoke: true, reason: 'user_changed' })
  })

  it('ensure for a different user turns alerts off instead of re-minting', async () => {
    native()
    localStorage.setItem(OWNER_KEY, OWNER)
    plugin.status.mockResolvedValue({ enabled: true, running: true, serverUrl: 'https://dispatch.example.com', tokenSavedAt: 0, notificationsAllowed: true })
    plugin.stop.mockResolvedValue({ enabled: false })
    const a = api()
    const res = await ensureOfficeAlerts(a, { userId: 'someone-else' })
    expect(res.enabled).toBe(false)
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.stop).toHaveBeenCalledWith({ revoke: true, reason: 'user_changed' })
  })

  it('ensure swallows a failed re-mint', async () => {
    native()
    plugin.status.mockResolvedValue({ enabled: true, running: true, serverUrl: 'https://dispatch.example.com', tokenSavedAt: 0, notificationsAllowed: true })
    mine()
    const a = { post: vi.fn().mockRejectedValue(new Error('offline')) }
    const res = await ensureOfficeAlerts(a, { userId: OWNER })
    expect(res.enabled).toBe(true)
  })

  // /audit 2026-10-14: an owner check and a re-mint running side by side
  // could undo each other.
  it('sync runs one call at a time, in order', async () => {
    native()
    mine()
    const order = []
    let release
    plugin.status
      .mockImplementationOnce(() => new Promise((r) => { release = () => { order.push('first done'); r({ enabled: true, running: true }) } }))
      .mockImplementation(async () => { order.push('second started'); return { enabled: false } })
    const first = syncOfficeAlerts(api(), { userId: OWNER })
    const second = syncOfficeAlerts(api(), { userId: OWNER, office: true })
    await Promise.resolve(); await Promise.resolve()
    expect(order).toEqual([])
    release()
    await Promise.all([first, second])
    expect(order).toEqual(['first done', 'second started'])
  })

  // /audit 2026-10-14: an owner check landing between the native save of a
  // fresh token and its owner's revoked the user's own new alerts.
  it('an owner check during turning alerts on waits, and keeps them', async () => {
    native()
    let started
    plugin.start.mockImplementation(() => new Promise((r) => { started = () => r({ enabled: true, running: true }) }))
    plugin.status.mockResolvedValue({ enabled: true, running: true })
    const enabling = enableOfficeAlerts(api())
    await new Promise((r) => setTimeout(r, 0))
    const checking = checkOfficeAlertsOwner(OWNER)
    await new Promise((r) => setTimeout(r, 0))
    expect(plugin.status).not.toHaveBeenCalled()
    started()
    await Promise.all([enabling, checking])
    expect(plugin.stop).not.toHaveBeenCalled()
    expect(localStorage.getItem(OWNER_KEY)).toBe(OWNER)
  })

  it('sync without office access only checks the owner', async () => {
    native()
    mine()
    plugin.status.mockResolvedValue({ enabled: true, running: false, serverUrl: 'https://dispatch.example.com', tokenSavedAt: 0, notificationsAllowed: true })
    const a = api()
    await syncOfficeAlerts(a, { userId: OWNER })
    expect(a.post).not.toHaveBeenCalled()
    expect(plugin.resume).not.toHaveBeenCalled()
    expect(plugin.stop).not.toHaveBeenCalled()
  })

  it('the bridge routes a tap like the bell and stops alerts on a chosen logout', async () => {
    native()
    plugin.stop.mockResolvedValue({})
    const router = { push: vi.fn().mockResolvedValue() }
    const settle = () => new Promise((r) => setTimeout(r, 0))  // alert changes run queued
    // A tap that cold-starts the app arrives before the app knows whether it
    // is GDX Planner; it is routed only once that is known.
    let modeKnown
    installOfficeAlertsBridge(router, new Promise((r) => { modeKnown = r }))
    listeners.alertTap({ category: 'sms' })
    await settle()
    expect(router.push).not.toHaveBeenCalled()
    modeKnown('planner')
    await settle()
    expect(router.push).toHaveBeenCalledWith('/mobile/sms')
    listeners.alertTap({ category: 'something-new' })
    await settle()
    expect(router.push).toHaveBeenLastCalledWith('/mobile')
    // A tap to the page already open is a duplicate navigation: vue-router
    // resolves it with a failure instead of navigating. It is still
    // announced, so the open page can act on it.
    const taps = []
    const onTap = (e) => taps.push(e.detail)
    window.addEventListener('gdx:alert-tap', onTap)
    router.push.mockResolvedValueOnce({ type: 16 })  // NavigationFailureType.duplicated
    listeners.alertTap({ category: 'planner_reminder' })
    await settle()
    window.removeEventListener('gdx:alert-tap', onTap)
    expect(taps).toEqual([{ category: 'planner_reminder', dest: '/mobile/planner?tab=tasks' }])
    window.dispatchEvent(new CustomEvent('gdx:auth-logout', { detail: { chosen: false } }))
    window.dispatchEvent(new CustomEvent('gdx:auth-logout'))
    await settle()
    expect(plugin.stop).not.toHaveBeenCalled()
    window.dispatchEvent(new CustomEvent('gdx:auth-logout', { detail: { chosen: true } }))
    await settle()
    expect(plugin.stop).toHaveBeenCalledWith({ revoke: true, reason: 'logout' })
  })
})
