/**
 * GDXA-1187: the Android app locks its session when it goes to the background
 * and shows the unlock gate when it comes back. Drives the real router guard.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const listeners = {};
vi.mock('@capacitor/core', async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    Capacitor: { ...real.Capacitor, isNativePlatform: () => true, getPlatform: () => 'android' },
  };
});
vi.mock('@capacitor/app', () => ({
  App: { addListener: vi.fn((event, fn) => { listeners[event] = fn; }) },
}));
vi.mock('../serverConfig', async (importOriginal) => ({
  ...(await importOriginal()),
  getServerUrl: () => 'https://dispatch.example.com',
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

// eslint-disable-next-line import/first
import { createAppRouter, SESSION_UNLOCK_PATH } from '../../router/index';
// eslint-disable-next-line import/first
import { installSessionLockLifecycle, withoutSessionLock, _resetSessionLockLifecycleForTests } from '../sessionLockLifecycle';
// eslint-disable-next-line import/first
import { isSessionLocked, setQuickPin, unlockSession } from '../nativeAuth';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe('session lock across pause and resume', () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    setActivePinia(createPinia());
    _resetSessionLockLifecycleForTests();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      const u = String(url);
      if (u.endsWith('/auth/refresh')) return jsonResponse(200, { access_token: 'restored-token' });
      if (u.endsWith('/api/users/me')) return jsonResponse(200, { id: 'u1', role: 'admin', must_change_password: false });
      return jsonResponse(200, { permissions: ['*'] });
    }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function signedInRouter() {
    const router = createAppRouter();
    installSessionLockLifecycle(router, SESSION_UNLOCK_PATH);
    await router.push('/dashboard').catch(() => {});
    // The PIN was set during this session (as setup would): unlocked, signed in.
    await setQuickPin('1234');
    expect(useAuthStore().isAuthenticated).toBe(true);
    expect(router.currentRoute.value.path).toBe('/dashboard');
    return router;
  }

  it('going to the background locks; coming back lands on the unlock screen', async () => {
    const router = await signedInRouter();
    listeners.pause();
    expect(isSessionLocked()).toBe(true);
    listeners.resume();
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(SESSION_UNLOCK_PATH));
    expect(router.currentRoute.value.query.redirect).toBe('/dashboard');
  });

  it('coming back unlocked stays where it was', async () => {
    const router = await signedInRouter();
    listeners.pause();
    unlockSession();
    listeners.resume();
    await new Promise((r) => { setTimeout(r, 20); });
    expect(router.currentRoute.value.path).toBe('/dashboard');
  });

  it('signed out, going to the background sets no lock', async () => {
    const router = await signedInRouter();
    useAuthStore().logout();
    await router.push('/login').catch(() => {});
    listeners.pause();
    expect(isSessionLocked()).toBe(false);
  });

  it('a camera intent wrapped in withoutSessionLock does not lock on the pause it causes', async () => {
    await signedInRouter();
    await withoutSessionLock(async () => { listeners.pause(); });
    expect(isSessionLocked()).toBe(false);
    listeners.pause();
    expect(isSessionLocked()).toBe(true);
  });
});
