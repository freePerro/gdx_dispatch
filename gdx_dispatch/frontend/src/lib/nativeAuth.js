import { Capacitor } from '@capacitor/core'
import { AccessControl, NativeBiometric } from '@capgo/capacitor-native-biometric'

const PIN_KEY = 'gdx_mobile_quick_pin_hash'
const PIN_ENABLED_KEY = 'gdx_mobile_quick_pin_enabled'
const BIOMETRIC_ENABLED_KEY = 'gdx_mobile_biometric_enabled'
// The lock flag lives in localStorage, where any script running in the page
// can clear it. That is a decision, not an oversight (GDXA-1187): the quick
// PIN is a CONVENIENCE gate against a casual glance at an unattended phone,
// not a security boundary. The session itself is guarded by the refresh
// cookie and the server; anything that needs a real boundary must not rest on
// this flag. The router (router/index.js) and lib/sessionLockLifecycle.js set
// it at cold start and on pause, and hold every non-public route on /unlock
// until verifyQuickPin() or performBiometricUnlock() clears it.
const LOCKED_KEY = 'gdx_mobile_session_locked'

/**
 * Simple SHA-256 hash using Web Crypto API.
 */
async function hashPin(pin) {
  const enc = new TextEncoder()
  const data = enc.encode(`gdx_pin_salt_${pin}`)
  const hashBuffer = await crypto.subtle.digest('SHA-256', data)
  const hashArray = Array.from(new Uint8Array(hashBuffer))
  return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('')
}

export function isQuickPinConfigured() {
  if (typeof window === 'undefined' || !window.localStorage) return false
  return Boolean(window.localStorage.getItem(PIN_KEY))
}

export function isQuickPinEnabled() {
  if (typeof window === 'undefined' || !window.localStorage) return false
  return window.localStorage.getItem(PIN_ENABLED_KEY) === 'true' && isQuickPinConfigured()
}

export async function setQuickPin(pin) {
  if (!pin || pin.length !== 4 || !/^\d{4}$/.test(pin)) {
    throw new Error('PIN must be exactly 4 digits')
  }
  const hash = await hashPin(pin)
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.setItem(PIN_KEY, hash)
    window.localStorage.setItem(PIN_ENABLED_KEY, 'true')
    window.localStorage.removeItem(LOCKED_KEY)
  }
  return true
}

export async function verifyQuickPin(pin) {
  if (!isQuickPinConfigured()) return false
  const expectedHash = window.localStorage.getItem(PIN_KEY)
  const candidateHash = await hashPin(pin)
  const match = candidateHash === expectedHash
  if (match) {
    unlockSession()
  }
  return match
}

export function clearQuickPin() {
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.removeItem(PIN_KEY)
    window.localStorage.removeItem(PIN_ENABLED_KEY)
    window.localStorage.removeItem(BIOMETRIC_ENABLED_KEY)
    window.localStorage.removeItem(LOCKED_KEY)
  }
}

export function lockSession() {
  if (typeof window !== 'undefined' && window.localStorage && isQuickPinEnabled()) {
    window.localStorage.setItem(LOCKED_KEY, 'true')
  }
}

export function unlockSession() {
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.removeItem(LOCKED_KEY)
  }
}

export function isSessionLocked() {
  if (typeof window === 'undefined' || !window.localStorage) return false
  return isQuickPinEnabled() && window.localStorage.getItem(LOCKED_KEY) === 'true'
}

/**
 * Biometric / Fingerprint Authentication
 */
export async function checkBiometricAvailability() {
  if (!Capacitor.isNativePlatform()) {
    return { available: false, reason: 'not_native' }
  }

  try {
    const result = await NativeBiometric.isAvailable()
    return {
      available: Boolean(result?.isAvailable),
      biometryType: result?.biometryType,
    }
  } catch (err) {
    console.warn('[nativeAuth] checkBiometricAvailability error:', err)
    return { available: false, error: err?.message || String(err) }
  }
}

export function isBiometricEnabled() {
  if (typeof window === 'undefined' || !window.localStorage) return false
  const setting = window.localStorage.getItem(BIOMETRIC_ENABLED_KEY)
  // Defaults to enabled if not explicitly disabled and PIN is enabled
  return setting !== 'false' && isQuickPinEnabled()
}

export function setBiometricEnabled(enabled) {
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.setItem(BIOMETRIC_ENABLED_KEY, String(Boolean(enabled)))
  }
}

export async function performBiometricUnlock({
  title = 'Unlock GDX Dispatch',
  subtitle = 'Confirm your fingerprint to resume your session',
  reason = 'Scan fingerprint to unlock GDX Dispatch',
} = {}) {
  if (!Capacitor.isNativePlatform()) {
    return { success: false, reason: 'not_native' }
  }

  try {
    const avail = await checkBiometricAvailability()
    if (!avail.available) {
      return { success: false, reason: 'biometrics_unavailable' }
    }

    await NativeBiometric.verifyIdentity({
      title,
      subtitle,
      reason,
      useFallback: true,
      maxAttempts: 3,
    })

    unlockSession()
    return { success: true }
  } catch (err) {
    const msg = (err?.message || '').toLowerCase()
    const cancelled = msg.includes('cancel') || msg.includes('user') || msg.includes('dismiss')
    return {
      success: false,
      cancelled,
      error: err?.message || String(err),
    }
  }
}

/**
 * Fingerprint sign-in
 *
 * The quick-unlock above only guards a session that is still alive; after a
 * real logout there is no session left to unlock. To sign back in with a
 * fingerprint the app keeps the email + password in the Android Keystore,
 * encrypted under a key that only a live biometric check can use
 * (AccessControl.BIOMETRY_ANY, authValidityDuration 0 — every read prompts).
 * Entries are keyed by server URL, so switching servers never replays one
 * server's password to another.
 */
export async function isFingerprintSignInSaved(server) {
  if (!Capacitor.isNativePlatform() || !server) return false
  try {
    const { isSaved } = await NativeBiometric.isCredentialsSaved({ server })
    return Boolean(isSaved)
  } catch {
    return false
  }
}

export async function saveFingerprintSignIn(server, email, password) {
  await NativeBiometric.setCredentials({
    server,
    username: email,
    password,
    accessControl: AccessControl.BIOMETRY_ANY,
    title: 'Turn on fingerprint sign-in',
    negativeButtonText: 'Not now',
  })
}

/** Resolves to { email, password }, or rejects if the prompt was cancelled. */
export async function readFingerprintSignIn(server) {
  const creds = await NativeBiometric.getSecureCredentials({
    server,
    title: 'Sign in to GDX Dispatch',
    subtitle: 'Use your fingerprint',
    reason: 'Sign in to GDX Dispatch',
    negativeButtonText: 'Use password',
  })
  return { email: creds.username, password: creds.password }
}

export async function forgetFingerprintSignIn(server) {
  if (!Capacitor.isNativePlatform() || !server) return
  try {
    await NativeBiometric.deleteCredentials({ server })
  } catch {
    /* nothing saved */
  }
}
