import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../src/stores/auth';

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it('login success stores access token in memory', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, { access_token: 'token-123' }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const auth = useAuthStore();
    await auth.login({ email: 'user@test.com', password: 'pw' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/auth/login',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(auth.accessToken).toBe('token-123');
    expect(auth.isAuthenticated).toBe(true);
  });

  it('login failure does not set token', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'bad creds' })));

    const auth = useAuthStore();
    await expect(auth.login({ email: 'user@test.com', password: 'wrong' })).rejects.toThrow(
      'bad creds',
    );

    expect(auth.accessToken).toBeNull();
    expect(auth.isAuthenticated).toBe(false);
  });

  it('login failure WIPES any prior session — kiosk safety', async () => {
    // 2026-05-09 incident: chrome-devtools browser carried a stale admin
    // token; a wrong-password login left the prior session intact, so the
    // route guard's `isAuthenticated = Boolean(accessToken.value)` stayed
    // true and /dashboard rendered as the prior admin. Same bug applies
    // to any shared kiosk: typing the wrong password into someone else's
    // session leaves you operating as them. The fix: every failure path
    // (4xx + network error) must call _clearSession + best-effort
    // /auth/logout.
    sessionStorage.setItem('gdx_access_token', 'prior-admin-token');
    sessionStorage.setItem('gdx_user', JSON.stringify({ id: 'prior-admin' }));
    sessionStorage.setItem('gdx_tenant_slug', 'gdx');

    setActivePinia(createPinia());
    const auth = useAuthStore();
    expect(auth.accessToken).toBe('prior-admin-token');
    expect(auth.isAuthenticated).toBe(true);

    const fetchMock = vi.fn(async (url) => {
      if (typeof url === 'string' && url.endsWith('/auth/login')) {
        return jsonResponse(401, { detail: 'Invalid credentials' });
      }
      if (typeof url === 'string' && url.endsWith('/auth/logout')) {
        return jsonResponse(200, { ok: true });
      }
      return jsonResponse(200, {});
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(auth.login({ email: 'attacker@x.com', password: 'wrong' })).rejects.toThrow(
      'Invalid credentials',
    );

    // Every trace of the prior session is gone.
    expect(auth.accessToken).toBeNull();
    expect(auth.user).toBeNull();
    expect(auth.isAuthenticated).toBe(false);
    expect(sessionStorage.getItem('gdx_access_token')).toBeNull();
    expect(sessionStorage.getItem('gdx_user')).toBeNull();
    expect(sessionStorage.getItem('gdx_tenant_slug')).toBeNull();

    // Server-side logout was called too — invalidates the HttpOnly cookie
    // so a re-auth can't skate by on the prior session's cookie.
    const calls = fetchMock.mock.calls.map((c) => c[0]);
    expect(calls).toContain('/auth/logout');
  });

  it('login network error WIPES prior session', async () => {
    // Same defense as above for the network-error path.
    sessionStorage.setItem('gdx_access_token', 'prior-admin-token');
    setActivePinia(createPinia());
    const auth = useAuthStore();
    expect(auth.isAuthenticated).toBe(true);

    const fetchMock = vi.fn(async (url) => {
      if (typeof url === 'string' && url.endsWith('/auth/login')) {
        throw new TypeError('Network request failed');
      }
      return jsonResponse(200, { ok: true });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(auth.login({ email: 'a@b.c', password: 'pw' })).rejects.toThrow(
      'Network request failed',
    );

    expect(auth.accessToken).toBeNull();
    expect(sessionStorage.getItem('gdx_access_token')).toBeNull();
  });

  it('logout clears token and user state', () => {
    const auth = useAuthStore();
    auth.accessToken = 'token-abc';
    auth.user = { id: 1, name: 'Jane' };

    auth.logout();

    expect(auth.accessToken).toBeNull();
    expect(auth.user).toBeNull();
    expect(auth.isAuthenticated).toBe(false);
  });

  it('refresh flow updates access token', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { access_token: 'refreshed' })));

    const auth = useAuthStore();
    auth.accessToken = 'old';
    const refreshed = await auth.refreshAccessToken();

    expect(refreshed).toBe('refreshed');
    expect(auth.accessToken).toBe('refreshed');
  });
});
