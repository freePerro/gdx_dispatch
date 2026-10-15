/**
 * A session that dies under an open page sends the person to log in.
 *
 * Before 2026-10-15 a refused token refresh logged the store out and threw a
 * status-less "Token refresh failed": useApi's 401 branch never ran, nothing
 * listened for `gdx:auth-logout` to navigate, and the dispatch board stayed
 * up stacking one generic "Error" toast per poll call. Pins:
 *   1. A refused refresh surfaces as a 401 "Session expired" error.
 *   2. The router sends an unchosen logout to /login?redirect=<where they were>
 *      &reason=expired, even when a background poll (createApiClient, no
 *      toast path) is the first call to meet the dead session.
 *   3. A chosen sign-out does not navigate from here (its caller does).
 *   4. A burst of dying calls shows no toast at all — the login page explains.
 *   5. A refresh lost to a network blip, or to a gateway 5xx mid-deploy,
 *      keeps the session and the page.
 *   6. A 401 on a public page (a visitor with no session) navigates nowhere
 *      and says nothing.
 *   7. A logout before the first page has loaded is left to the guard, which
 *      keeps the real destination (the native cold-start restore).
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { defineComponent, h } from 'vue';
import { flushPromises, mount } from '@vue/test-utils';
import { createAppRouter } from '../index';
import { useAuthStore } from '../../stores/auth';
import { createApiClient, useApi } from '../../composables/useApi';

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

// Every /api call 401s; /auth/refresh answers `refresh` (a status, or a
// function that throws for a network failure).
function stubServer({ refresh = 401 } = {}) {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const u = String(url);
    if (u === '/auth/refresh') {
      if (typeof refresh === 'function') return refresh();
      return jsonResponse(refresh, { detail: 'Invalid refresh token' });
    }
    if (u === '/auth/logout') return jsonResponse(200, {});
    if (u.startsWith('/api/users/me/permissions')) return jsonResponse(200, { permissions: ['*'] });
    if (u.startsWith('/api/feedback/')) return jsonResponse(200, {});
    return jsonResponse(401, { detail: 'Invalid or expired access token' });
  }));
}

describe('expired session → login', () => {
  let auth;

  beforeEach(() => {
    sessionStorage.clear();
    setActivePinia(createPinia());
    auth = useAuthStore();
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'admin' };
    toastAdd.mockClear();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('turns a refused refresh into a 401 "Session expired" and logs out', async () => {
    stubServer();
    const err = await createApiClient().get('/api/dispatch/jobs').catch((e) => e);
    expect(err.status).toBe(401);
    expect(err.message).toBe('Session expired');
    expect(auth.isAuthenticated).toBe(false);
  });

  it('sends the person to /login with a way back to where they were', async () => {
    stubServer({ refresh: 200 });
    const router = createAppRouter();
    await router.push('/dashboard?tab=week');
    expect(router.currentRoute.value.path).toBe('/dashboard');

    stubServer();
    await createApiClient().get('/api/dispatch/jobs').catch(() => {});
    await flushPromises();

    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query.redirect).toBe('/dashboard?tab=week');
    expect(router.currentRoute.value.query.reason).toBe('expired');
  });

  it('sends a component call that meets the dead session to the same login page', async () => {
    stubServer({ refresh: 200 });
    const router = createAppRouter();
    await router.push('/dashboard');
    const captured = {};
    mount(defineComponent({
      setup() { captured.api = useApi(); return () => h('div'); },
    }), { global: { plugins: [router] } });

    stubServer();
    await captured.api.get('/api/dispatch/jobs').catch(() => {});
    await flushPromises();

    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query).toMatchObject({ redirect: '/dashboard', reason: 'expired' });
    expect(toastAdd).not.toHaveBeenCalled();
  });

  it('leaves a chosen sign-out to its own caller', async () => {
    stubServer({ refresh: 200 });
    const router = createAppRouter();
    await router.push('/dashboard');
    const push = vi.spyOn(router, 'push');

    auth.logout({ chosen: true });
    await flushPromises();

    expect(push).not.toHaveBeenCalled();
  });

  it('shows no toast for a burst of dying calls', async () => {
    stubServer();
    const api = useApi();
    await Promise.all([
      api.get('/api/dispatch/jobs').catch(() => {}),
      api.get('/api/dispatch/scheduled-unassigned').catch(() => {}),
      api.get('/api/technicians').catch(() => {}),
    ]);
    await api.get('/api/dispatch/unassigned').catch(() => {});

    expect(toastAdd.mock.calls.map(([t]) => t.summary)).toEqual([]);
  });

  it('keeps the session and the page when the refresh is lost to the network', async () => {
    stubServer({ refresh: 200 });
    const router = createAppRouter();
    await router.push('/dashboard');

    stubServer({ refresh: () => { throw new TypeError('Failed to fetch'); } });
    const err = await createApiClient().get('/api/dispatch/jobs').catch((e) => e);
    await flushPromises();

    expect(err.message).toBe('Failed to fetch');
    expect(err.status).toBeUndefined();
    expect(auth.isAuthenticated).toBe(true);
    expect(router.currentRoute.value.path).toBe('/dashboard');
  });

  it('keeps the session and the page when the refresh gets a gateway 502', async () => {
    stubServer({ refresh: 200 });
    const router = createAppRouter();
    await router.push('/dashboard');

    stubServer({ refresh: 502 });
    const err = await createApiClient().get('/api/dispatch/jobs').catch((e) => e);
    await flushPromises();

    expect(err.status).toBe(502);
    expect(auth.isAuthenticated).toBe(true);
    expect(router.currentRoute.value.path).toBe('/dashboard');
  });

  it('does not send a visitor on a public page to /login', async () => {
    auth.accessToken = null;
    auth.user = null;
    stubServer();
    const router = createAppRouter();
    await router.push('/forgot-password');
    const captured = {};
    mount(defineComponent({
      setup() { captured.api = useApi(); return () => h('div'); },
    }), { global: { plugins: [router] } });

    await captured.api.get('/api/settings/branding').catch(() => {});
    await flushPromises();

    expect(router.currentRoute.value.path).toBe('/forgot-password');
    expect(toastAdd).not.toHaveBeenCalled();
  });

  it('leaves a logout before the first page loads to the guard, keeping the destination', async () => {
    const router = createAppRouter();
    stubServer();
    // The native cold start: the first navigation is still pending when the
    // refresh is refused and the store logs out.
    const first = router.push('/forgot-password');
    auth.logout();
    await first;
    await flushPromises();

    expect(router.currentRoute.value.path).toBe('/forgot-password');
  });
});
