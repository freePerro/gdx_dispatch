import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Capacitor } from '@capacitor/core'
import { PushNotifications } from '@capacitor/push-notifications'
import {
  isNativePush,
  getCurrentPushToken,
  createDefaultNotificationChannels,
  initPushNotifications,
  teardownPushNotifications,
} from '../nativePush.js'

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: vi.fn(),
  },
}))

vi.mock('@capacitor/push-notifications', () => ({
  PushNotifications: {
    createChannel: vi.fn().mockResolvedValue(undefined),
    checkPermissions: vi.fn(),
    requestPermissions: vi.fn(),
    register: vi.fn().mockResolvedValue(undefined),
    removeAllListeners: vi.fn().mockResolvedValue(undefined),
    addListener: vi.fn(),
  },
}))

describe('nativePush', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('reports isNativePush based on Capacitor platform', () => {
    Capacitor.isNativePlatform.mockReturnValue(false)
    expect(isNativePush()).toBe(false)

    Capacitor.isNativePlatform.mockReturnValue(true)
    expect(isNativePush()).toBe(true)
  })

  it('gracefully exits initPushNotifications on non-native platform', async () => {
    Capacitor.isNativePlatform.mockReturnValue(false)
    const res = await initPushNotifications()
    expect(res).toEqual({ registered: false, reason: 'not_native' })
    expect(PushNotifications.register).not.toHaveBeenCalled()
  })

  it('creates notification channel on Android', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    await createDefaultNotificationChannels()
    expect(PushNotifications.createChannel).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'dispatch_alerts',
        name: 'Dispatch & Work Orders',
        importance: 4,
      })
    )
  })

  it('handles permission denied cleanly', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    PushNotifications.checkPermissions.mockResolvedValue({ receive: 'prompt' })
    PushNotifications.requestPermissions.mockResolvedValue({ receive: 'denied' })

    const res = await initPushNotifications()
    expect(res).toEqual({ registered: false, reason: 'permission_denied' })
    expect(PushNotifications.register).not.toHaveBeenCalled()
  })

  it('registers listeners and invokes callbacks on event triggers', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    PushNotifications.checkPermissions.mockResolvedValue({ receive: 'granted' })

    const listeners = {}
    PushNotifications.addListener.mockImplementation((event, callback) => {
      listeners[event] = callback
      return Promise.resolve({ remove: vi.fn() })
    })

    const onToken = vi.fn()
    const onNotification = vi.fn()
    const onAction = vi.fn()
    const router = { push: vi.fn() }

    const res = await initPushNotifications({ onToken, onNotification, onAction, router })
    expect(res).toEqual({ registered: true })
    expect(PushNotifications.register).toHaveBeenCalled()

    // 1. Test token registration
    listeners['registration']({ value: 'fcm-device-token-123' })
    expect(getCurrentPushToken()).toBe('fcm-device-token-123')
    expect(onToken).toHaveBeenCalledWith('fcm-device-token-123')

    // 2. Test foreground notification received
    const sampleNotification = { title: 'New Job Assigned', body: 'Repair opener at 123 Elm' }
    listeners['pushNotificationReceived'](sampleNotification)
    expect(onNotification).toHaveBeenCalledWith(sampleNotification)

    // 3. Test notification click with job_id
    listeners['pushNotificationActionPerformed']({
      notification: { data: { job_id: 'job-987' } },
    })
    expect(onAction).toHaveBeenCalled()
    expect(router.push).toHaveBeenCalledWith('/jobs/job-987')

    // 4. Test notification click with explicit url
    listeners['pushNotificationActionPerformed']({
      notification: { data: { url: '/chat/room-456' } },
    })
    expect(router.push).toHaveBeenCalledWith('/chat/room-456')
  })

  it('teardown cleans up listeners and clears token', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    await teardownPushNotifications()
    expect(PushNotifications.removeAllListeners).toHaveBeenCalled()
    expect(getCurrentPushToken()).toBeNull()
  })
})
