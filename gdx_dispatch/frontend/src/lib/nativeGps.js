import { Capacitor, registerPlugin } from '@capacitor/core'
import { Geolocation } from '@capacitor/geolocation'

// Native GpsForeground plugin registered in Android Bridge
const GpsForeground = registerPlugin('GpsForeground')

const LOCATION_DISCLOSURE_KEY = 'gdx_location_disclosure_acknowledged'

export function isNativeGps() {
  return Capacitor.isNativePlatform()
}

/**
 * Google Play Prominent Disclosure State.
 * Verifies whether the technician has affirmatively accepted the in-app
 * location tracking disclosure before initiating foreground or background location services.
 */
export function hasAcknowledgedLocationDisclosure() {
  if (typeof window === 'undefined' || !window.localStorage) return false
  return window.localStorage.getItem(LOCATION_DISCLOSURE_KEY) === 'true'
}

export function acknowledgeLocationDisclosure() {
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.setItem(LOCATION_DISCLOSURE_KEY, 'true')
  }
}

export function isLocationDisclosureNeeded() {
  return isNativeGps() && !hasAcknowledgedLocationDisclosure()
}

/**
 * Starts the persistent Android Foreground Service with an ongoing notification.
 * This guarantees the OS will not kill or freeze GPS streaming while the tech
 * is clocked in, even with the screen locked or app backgrounded.
 */
export async function startForegroundGps({
  title = 'GDX Dispatch',
  text = 'GPS tracking active while clocked in',
} = {}) {
  if (!isNativeGps()) return { running: false }

  try {
    // Request location permissions if supported by plugin
    if (typeof GpsForeground.requestPermissions === 'function') {
      try {
        await GpsForeground.requestPermissions()
      } catch {
        /* proceed to start */
      }
    }
    const res = await GpsForeground.start({ title, text })
    return res || { running: true }
  } catch (err) {
    console.warn('Could not start native GPS foreground service:', err)
    return { running: false, error: err?.message || String(err) }
  }
}

/**
 * Stops the persistent Foreground Service and removes the ongoing notification.
 * Called immediately when the tech clocks out, when a 403 response is returned,
 * or when the composable unmounts.
 */
export async function stopForegroundGps() {
  if (!isNativeGps()) return { running: false }

  try {
    const res = await GpsForeground.stop()
    return res || { running: false }
  } catch (err) {
    console.warn('Could not stop native GPS foreground service:', err)
    return { running: false, error: err?.message || String(err) }
  }
}

export async function isForegroundGpsRunning() {
  if (!isNativeGps()) return false
  try {
    const res = await GpsForeground.isRunning()
    return Boolean(res?.running)
  } catch {
    return false
  }
}

/**
 * Gets high-accuracy position.
 * On native platforms, attempts @capacitor/geolocation first.
 * If that times out or is unavailable, queries the native LocationManager last known location.
 * On web, falls back cleanly to navigator.geolocation.
 */
export async function getGpsPosition(options = { timeout: 15_000, maximumAge: 5_000, enableHighAccuracy: true }) {
  if (isNativeGps()) {
    try {
      const pos = await Geolocation.getCurrentPosition({
        enableHighAccuracy: options.enableHighAccuracy ?? true,
        timeout: options.timeout ?? 15_000,
        maximumAge: options.maximumAge ?? 5_000,
      })
      return {
        coords: {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          accuracy: pos.coords.accuracy ?? null,
          speed: pos.coords.speed ?? null,
          heading: pos.coords.heading ?? null,
        },
        timestamp: pos.timestamp || Date.now(),
      }
    } catch {
      // Direct LocationManager fallback on native platform
      try {
        const last = await GpsForeground.getLastLocation()
        if (last?.coords) {
          return {
            coords: {
              latitude: last.coords.latitude,
              longitude: last.coords.longitude,
              accuracy: last.coords.accuracy ?? null,
              speed: last.coords.speed ?? null,
              heading: last.coords.heading ?? null,
            },
            timestamp: last.timestamp || Date.now(),
          }
        }
      } catch {
        /* proceed to web fallback */
      }
    }
  }

  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator)) {
      reject(new Error('geolocation_unsupported'))
      return
    }
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: options.enableHighAccuracy ?? true,
      timeout: options.timeout ?? 15_000,
      maximumAge: options.maximumAge ?? 5_000,
    })
  })
}
