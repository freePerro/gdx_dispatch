/**
 * Sprint tech_mobile Phase 3 (S3-A5) — reactive online state singleton.
 *
 * Distinct from useOfflineSync: this is the read-only window-level
 * online/offline observer. Singleton so every component gets the same
 * ref and listeners are attached exactly once.
 *
 * On native Android (Capacitor), uses @capacitor/network for immediate,
 * accurate ConnectivityManager updates across cellular and WiFi.
 * On web, falls back cleanly to window 'online'/'offline' events and
 * document.visibilitychange.
 */
import { ref } from 'vue'
import { Capacitor } from '@capacitor/core'
import { Network } from '@capacitor/network'

const isOnline = ref(typeof navigator !== 'undefined' ? navigator.onLine : true)
const connectionType = ref('unknown')
let _attached = false

async function _attachNative() {
  try {
    const status = await Network.getStatus()
    isOnline.value = Boolean(status.connected)
    connectionType.value = status.connectionType || 'unknown'

    await Network.addListener('networkStatusChange', (status) => {
      isOnline.value = Boolean(status.connected)
      connectionType.value = status.connectionType || 'unknown'
    })
  } catch (err) {
    console.warn('[useOnlineState] Failed to attach native Network listener:', err)
  }
}

function _attachWeb() {
  try {
    if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
      window.addEventListener('online', () => { isOnline.value = true })
      window.addEventListener('offline', () => { isOnline.value = false })
    }
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden && typeof navigator !== 'undefined') {
          isOnline.value = navigator.onLine
        }
      })
    }
  } catch {
    // Non-DOM env (vitest with minimal stubs) — leave singleton at its default.
  }
}

function _attach() {
  if (_attached) return
  _attached = true

  if (Capacitor.isNativePlatform()) {
    _attachNative()
  } else {
    _attachWeb()
  }
}

export function useOnlineState() {
  _attach()
  return { isOnline, connectionType }
}
