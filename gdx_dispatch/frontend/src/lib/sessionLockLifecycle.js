/**
 * Locks the Android app's session when it goes to the background, and shows
 * the unlock gate when it comes back (GDXA-1187). The cold-start half lives in
 * the router guard, which locks before it restores the session.
 *
 * Both are no-ops unless the tech has turned the quick PIN on: lockSession()
 * and isSessionLocked() read that setting (lib/nativeAuth.js).
 */
import { Capacitor } from '@capacitor/core';
import { App } from '@capacitor/app';
import { isSessionLocked, lockSession } from './nativeAuth';
import { useAuthStore } from '../stores/auth';

let _installed = false;
let _externalActivity = 0;

/**
 * Runs fn (a system camera or scanner intent) without locking the session on
 * the pause it causes: Android pauses the app for the intent, and locking
 * there would hold the tech on /unlock and tear down the page mid-capture.
 */
export async function withoutSessionLock(fn) {
  _externalActivity += 1;
  try {
    return await fn();
  } finally {
    _externalActivity -= 1;
  }
}

export function installSessionLockLifecycle(router, unlockPath) {
  if (_installed || !Capacitor.isNativePlatform()) return;
  _installed = true;

  App.addListener('pause', () => {
    if (_externalActivity > 0) return;
    // Signed out there is no session to guard; a lock would only hold the
    // next password sign-in (the guard drops it anyway).
    if (useAuthStore().isAuthenticated) lockSession();
  });

  App.addListener('resume', () => {
    if (!isSessionLocked()) return;
    const current = router.currentRoute.value;
    if (current.path === unlockPath || current.meta?.public) return;
    router.push({ path: unlockPath, query: { redirect: current.fullPath } }).catch(() => {});
  });
}

/** Test seam: forget the install so a spec can install again. */
export function _resetSessionLockLifecycleForTests() {
  _installed = false;
}
