import { Capacitor } from '@capacitor/core'
import { PushNotifications } from '@capacitor/push-notifications'

let currentToken = null

export function isNativePush() {
  return Capacitor.isNativePlatform()
}

export function getCurrentPushToken() {
  return currentToken
}

/**
 * Configure default notification channels for Android.
 * In Android 8+, notifications require a channel to display.
 */
export async function createDefaultNotificationChannels() {
  if (!isNativePush() || typeof PushNotifications.createChannel !== 'function') {
    return
  }

  try {
    await PushNotifications.createChannel({
      id: 'dispatch_alerts',
      name: 'Dispatch & Work Orders',
      description: 'Alerts for assigned jobs, schedule changes, and urgent messages',
      importance: 4, // HIGH
      visibility: 1, // PUBLIC
      vibration: true,
      lights: true,
      lightColor: '#2563eb',
    })
  } catch (err) {
    console.warn('[nativePush] Could not create notification channel:', err)
  }
}

/**
 * Initializes native push notification listeners, registers for FCM,
 * and sets up deep-linking / navigation on notification tap.
 *
 * @param {Object} options
 * @param {Function} [options.onToken] Callback when device token is received
 * @param {Function} [options.onNotification] Callback when notification is received in foreground
 * @param {Function} [options.onAction] Callback when user taps notification
 * @param {Object} [options.router] Vue Router instance for deep linking
 * @returns {Promise<{ registered: boolean, token?: string, error?: string }>}
 */
export async function initPushNotifications({
  onToken,
  onNotification,
  onAction,
  router,
} = {}) {
  if (!isNativePush()) {
    return { registered: false, reason: 'not_native' }
  }

  try {
    await createDefaultNotificationChannels()

    let permStatus = await PushNotifications.checkPermissions()
    if (permStatus.receive === 'prompt' || permStatus.receive === 'prompt-with-rationale') {
      permStatus = await PushNotifications.requestPermissions()
    }

    if (permStatus.receive !== 'granted') {
      return { registered: false, reason: 'permission_denied' }
    }

    // Clear any previous listeners
    await PushNotifications.removeAllListeners()

    // 1. Device Token Registration
    await PushNotifications.addListener('registration', (token) => {
      currentToken = token?.value || null
      if (typeof onToken === 'function' && currentToken) {
        onToken(currentToken)
      }
    })

    // 2. Registration Error
    await PushNotifications.addListener('registrationError', (error) => {
      console.warn('[nativePush] FCM registration failed:', error)
    })

    // 3. Foreground Notification
    await PushNotifications.addListener('pushNotificationReceived', (notification) => {
      if (typeof onNotification === 'function') {
        onNotification(notification)
      }
    })

    // 4. Notification Click / Action Performed
    await PushNotifications.addListener('pushNotificationActionPerformed', (action) => {
      const data = action?.notification?.data || {}
      if (typeof onAction === 'function') {
        onAction(action)
      }

      // Handle automatic navigation if router is supplied
      if (router) {
        if (data.url) {
          router.push(data.url)
        } else if (data.job_id) {
          router.push(`/jobs/${data.job_id}`)
        }
      }
    })

    // Trigger registration with FCM
    await PushNotifications.register()
    return { registered: true }
  } catch (err) {
    console.warn('[nativePush] Failed to init push notifications:', err)
    return { registered: false, error: err?.message || String(err) }
  }
}

/**
 * Removes all push notification listeners.
 */
export async function teardownPushNotifications() {
  if (!isNativePush()) return
  try {
    await PushNotifications.removeAllListeners()
    currentToken = null
  } catch (err) {
    console.warn('[nativePush] Teardown error:', err)
  }
}
