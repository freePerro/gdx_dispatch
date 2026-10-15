/**
 * The Android app loses sessionStorage every time it is closed, so the router
 * restores the session once per launch from the native refresh cookie. That
 * also loses the stored must_change_password hold: the restore re-asks
 * /api/users/me, so a flagged account is held on the change-password form
 * from the first screen, not after its first refused call (/audit 2026-10-14).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

vi.mock('@capacitor/core', async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    Capacitor: { ...real.Capacitor, isNativePlatform: () => true, getPlatform: () => 'android' },
  };
});
vi.mock('../../lib/serverConfig', async (importOriginal) => ({
  ...(await importOriginal()),
  getServerUrl: () => 'https://dispatch.example.com',
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

// eslint-disable-next-line import/first
import { createAppRouter, PASSWORD_CHANGE_PATH } from '../index';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

function serve(me) {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const u = String(url);
    if (u.endsWith('/auth/refresh')) return jsonResponse(200, { access_token: 'restored-token' });
    if (u.endsWith('/api/users/me')) return jsonResponse(200, me);
    return jsonResponse(200, { permissions: ['*'] });
  }));
}

describe('native cold start: session restore', () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    setActivePinia(createPinia());
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('a restored account that must change its password is held on the form from the first screen', async () => {
    serve({ id: 'u1', role: 'admin', must_change_password: true });
    const router = createAppRouter();
    await router.push('/dashboard').catch(() => {});
    const auth = useAuthStore();
    expect(auth.isAuthenticated).toBe(true);
    expect(auth.mustChangePassword).toBe(true);
    expect(router.currentRoute.value.path).toBe(PASSWORD_CHANGE_PATH);
  });

  it('a second navigation during the restore waits for it, not the login page', async () => {
    let refreshed;
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      const u = String(url);
      if (u.endsWith('/auth/refresh')) {
        await new Promise((r) => { refreshed = r; });
        return jsonResponse(200, { access_token: 'restored-token' });
      }
      if (u.endsWith('/api/users/me')) return jsonResponse(200, { id: 'u1', role: 'admin', must_change_password: false });
      return jsonResponse(200, { permissions: ['*'] });
    }));
    const router = createAppRouter();
    const first = router.push('/dashboard').catch(() => {});
    await vi.waitFor(() => expect(refreshed).toBeTypeOf('function'));
    const second = router.push('/mobile').catch(() => {});
    await new Promise((r) => { setTimeout(r, 50); }); // the second guard runs first
    refreshed();
    await Promise.all([first, second]);
    expect(useAuthStore().isAuthenticated).toBe(true);
    expect(router.currentRoute.value.path).toBe('/mobile');
  });

  it('a /me that fails does not block the restore', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      const u = String(url);
      if (u.endsWith('/auth/refresh')) return jsonResponse(200, { access_token: 'restored-token' });
      if (u.endsWith('/api/users/me')) return jsonResponse(500, { detail: 'boom' });
      return jsonResponse(200, { permissions: ['*'] });
    }));
    const router = createAppRouter();
    await router.push('/dashboard').catch(() => {});
    expect(useAuthStore().isAuthenticated).toBe(true);
    expect(router.currentRoute.value.path).toBe('/dashboard');
  });

  it('a restored account with nothing to change goes where it asked', async () => {
    serve({ id: 'u1', role: 'admin', must_change_password: false });
    const router = createAppRouter();
    await router.push('/dashboard').catch(() => {});
    expect(useAuthStore().isAuthenticated).toBe(true);
    expect(router.currentRoute.value.path).toBe('/dashboard');
  });
});
