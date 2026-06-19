/**
 * Regression — logout() MUST hit the server so the HttpOnly refresh
 * cookie gets a Set-Cookie Max-Age=0.
 *
 * 2026-05-14 incident: logout() only cleared sessionStorage. The
 * refresh_token cookie (httpOnly + domain=.example.com) can't be
 * deleted by JS, so a refresh-failure logout left the dead cookie in
 * place. The next background refresh resent it, the server's replay
 * detector 401'd + sank a server_error, logout() fired again as a
 * no-op, and the loop reflooded CC support/errors (6,717 rows / 4 days,
 * same jti replayed 1,526×). The fix wires logout() → _serverLogout()
 * (POST /auth/logout). If a refactor ever unwires it, this fails.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

beforeEach(() => {
  setActivePinia(createPinia());
  sessionStorage.clear();
  global.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }),
  );
  vi.resetModules();
});

describe('auth store — logout clears the server-side refresh cookie', () => {
  it('logout() POSTs /auth/logout with credentials so the cookie is revoked', async () => {
    const { useAuthStore } = await import('../stores/auth');
    const store = useAuthStore();

    store.logout();

    const calls = global.fetch.mock.calls;
    const logoutCall = calls.find(([url]) => url === '/auth/logout');
    expect(logoutCall, 'logout() must POST /auth/logout').toBeTruthy();
    expect(logoutCall[1].method).toBe('POST');
    expect(logoutCall[1].credentials).toBe('include');
    // keepalive is load-bearing: logout() doesn't await this and the
    // refresh-401 caller throws + navigates immediately. Without it the
    // browser cancels the request on unload and the cookie is never
    // revoked — the dead-cookie loop returns. Pin it structurally.
    expect(logoutCall[1].keepalive, 'logout POST must use keepalive').toBe(true);
  });

  it('logout() still clears local session (no regression on the local guarantee)', async () => {
    sessionStorage.setItem('gdx_access_token', 'eyJhbGciOiJ.fake.sig');
    const { useAuthStore } = await import('../stores/auth');
    const store = useAuthStore();
    expect(store.isAuthenticated).toBe(true);

    store.logout();

    expect(store.isAuthenticated).toBe(false);
    expect(sessionStorage.getItem('gdx_access_token')).toBeNull();
  });

  it('a failed refresh logs out AND revokes the cookie — breaks the replay loop', async () => {
    global.fetch = vi.fn((url) => {
      if (url === '/auth/refresh') {
        return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
    });
    const { useAuthStore } = await import('../stores/auth');
    const store = useAuthStore();

    await expect(store.refreshAccessToken()).rejects.toThrow();

    const hitLogout = global.fetch.mock.calls.some(([url]) => url === '/auth/logout');
    expect(hitLogout, 'a 401 from /auth/refresh must trigger server logout').toBe(true);
  });
});
