/**
 * Sprint tech_mobile Phase 3 (S3-A5) — reactive online state singleton.
 *
 * Distinct from useOfflineSync: this is the read-only window-level
 * online/offline observer. Singleton so every component gets the same
 * ref and listeners are attached exactly once.
 *
 * Triggers:
 *   - window 'online' / 'offline' events
 *   - document.visibilitychange (re-checks navigator.onLine on resume —
 *     iOS occasionally drops "offline" events when the app backgrounds)
 */
import { ref } from 'vue'

const isOnline = ref(typeof navigator !== 'undefined' ? navigator.onLine : true)
let _attached = false

function _attach() {
  if (_attached) return
  _attached = true
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

export function useOnlineState() {
  _attach()
  return { isOnline }
}
