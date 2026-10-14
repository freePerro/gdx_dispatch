/**
 * must_change_password routing — GDXA-1004.
 *
 * The login response carried `user.must_change_password` and nothing read it,
 * so the seeded admin's log-printed bootstrap password stayed usable forever.
 * The server (GDXA-1003) refuses a flagged session with 403
 * `{code: "password_change_required", message}` and clears the flag on a
 * password change or reset; a bare-string detail is pinned too. The SPA's
 * half is to send the person to the change-password form (/profile) and keep
 * them there until the change succeeds. Pins:
 *   1. A flagged session is redirected to /profile from any shell route,
 *      ahead of the tech/mobile redirects; /profile itself goes through.
 *   2. Public routes stay reachable (login, reset-password) — no trap.
 *   3. An unflagged session is untouched.
 *   4. A 403 `password_change_required` from any API call (bare string or
 *      `{code, message}`) flags the session and moves it to /profile.
 *   5. A successful change-password POST clears the flag and releases the guard.
 *   6. hydrateUser and any GET /api/users/me (which /profile makes on every
 *      load) take the flag from the server, and keep it when absent — so a
 *      reset by link or a change in another tab releases this tab too.
 *   7. Leaving /profile re-asks /api/users/me first, so a stale flag (changed
 *      elsewhere, or set again by a 403 that landed after the change) lets
 *      the person go on their next click instead of only after a reload.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createAppRouter, PASSWORD_CHANGE_PATH } from '../index';
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
  };
}

describe('must_change_password routing', () => {
  let auth;

  beforeEach(() => {
    sessionStorage.clear();
    setActivePinia(createPinia());
    auth = useAuthStore();
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Initial Admin', role: 'admin', must_change_password: true };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, { permissions: ['*'] })));
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('holds the change-password form at /profile', () => {
    expect(PASSWORD_CHANGE_PATH).toBe('/profile');
  });

  it.each(['/dashboard', '/billing', '/customers', '/mobile', '/settings'])(
    'redirects a flagged session from %s to /profile',
    async (path) => {
      const router = createAppRouter();
      await router.push(path);
      expect(router.currentRoute.value.path).toBe('/profile');
    },
  );

  it('lets a flagged session open /profile', async () => {
    const router = createAppRouter();
    await router.push('/profile');
    expect(router.currentRoute.value.name).toBe('profile');
  });

  it('runs before the technician redirect, which would bounce a tech to /mobile', async () => {
    auth.user = { name: 'New Tech', role: 'technician', must_change_password: true };
    const router = createAppRouter();
    await router.push('/dashboard');
    expect(router.currentRoute.value.path).toBe('/profile');
  });

  it('leaves public routes reachable for a flagged session', async () => {
    const router = createAppRouter();
    await router.push('/reset-password');
    expect(router.currentRoute.value.name).toBe('reset-password');
  });

  it('does not touch an unflagged session', async () => {
    auth.user = { name: 'Office', role: 'admin', must_change_password: false };
    const router = createAppRouter();
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');
  });

  it.each([
    ['a bare detail string', { detail: 'password_change_required' }],
    ['a structured detail', { detail: { code: 'password_change_required', message: 'Change your password' } }],
  ])('flags the session and moves to /profile on a 403 with %s', async (_label, body) => {
    auth.user = { name: 'Office', role: 'admin', must_change_password: false };
    const router = createAppRouter();
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');

    fetch.mockImplementation(async (url) => (
      url === '/api/customers' ? jsonResponse(403, body) : jsonResponse(200, {})
    ));
    const api = createApiClient();
    const err = await api.get('/api/customers').catch((e) => e);
    expect(err.status).toBe(403);
    expect(err.code).toBe('password_change_required');
    // An expected refusal: not reported to the client-error tracker.
    expect(fetch).not.toHaveBeenCalledWith('/api/feedback/client-error', expect.anything());
    expect(auth.mustChangePassword).toBe(true);
    expect(JSON.parse(sessionStorage.getItem('gdx_user')).must_change_password).toBe(true);

    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/profile'));
  });

  it('shows no toast for the refusal (the layout banner says it once)', async () => {
    toastAdd.mockClear();
    fetch.mockImplementation(async () => jsonResponse(403, { detail: { code: 'password_change_required', message: 'x' } }));
    await useApi().get('/api/notifications/unread').catch(() => {});
    expect(toastAdd).not.toHaveBeenCalled();
    // ...while a plain permission 403 still toasts.
    fetch.mockImplementation(async () => jsonResponse(403, { detail: 'Missing permission: [invoices.read_all]' }));
    await useApi().get('/api/invoices').catch(() => {});
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Permission denied' }));
  });

  it('leaves a plain permission 403 alone', async () => {
    auth.user = { name: 'Office', role: 'admin', must_change_password: false };
    fetch.mockImplementation(async () => jsonResponse(403, { detail: 'Missing permission: [invoices.read_all]' }));
    const err = await createApiClient().get('/api/invoices').catch((e) => e);
    expect(err.status).toBe(403);
    expect(auth.mustChangePassword).toBe(false);
  });

  it('clears the flag on a successful change and releases the guard', async () => {
    const router = createAppRouter();
    await router.push('/dashboard');
    expect(router.currentRoute.value.path).toBe('/profile');

    fetch.mockImplementation(async () => jsonResponse(200, { ok: 'password_changed' }));
    await createApiClient().post('/api/users/me/change-password', {
      current_password: 'bootstrap', new_password: 'a-new-password',
    });
    expect(auth.mustChangePassword).toBe(false);
    expect(JSON.parse(sessionStorage.getItem('gdx_user')).must_change_password).toBe(false);
    // Permissions 403'd while flagged; they are reloaded once it clears.
    expect(fetch).toHaveBeenCalledWith('/api/users/me/permissions', expect.anything());

    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');
  });

  it('keeps the flag when a failed change is refused', async () => {
    fetch.mockImplementation(async () => jsonResponse(400, { detail: 'Current password is incorrect' }));
    await createApiClient().post('/api/users/me/change-password', {
      current_password: 'wrong', new_password: 'a-new-password',
    }).catch(() => {});
    expect(auth.mustChangePassword).toBe(true);
  });

  it('a forced hydrate that reads the flag cleared from /api/users/me releases the guard', async () => {
    fetch.mockImplementation(async () => jsonResponse(200, { id: 'u1', email: 'a@example.test', name: 'Initial Admin', role: 'admin', must_change_password: false }));
    await auth.hydrateUser({ force: true });
    expect(auth.mustChangePassword).toBe(false);
    const router = createAppRouter();
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');
  });

  it('releases the guard when /profile reads the flag cleared elsewhere from GET /api/users/me', async () => {
    // A reset link or another tab cleared it server-side; this tab still says true.
    const router = createAppRouter();
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/profile');
    fetch.mockImplementation(async () => jsonResponse(200, { id: 'u1', role: 'admin', must_change_password: false }));
    const me = await createApiClient().get('/api/users/me');
    expect(me.must_change_password).toBe(false);
    expect(auth.mustChangePassword).toBe(false);
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');
  });

  it('re-asks /api/users/me when a held tab tries to leave /profile, and lets it go without a reload', async () => {
    const router = createAppRouter();
    await router.push('/profile');
    // Cleared elsewhere; nothing in this tab has reloaded /profile.
    fetch.mockImplementation(async (url) => (
      url === '/api/users/me'
        ? jsonResponse(200, { id: 'u1', email: 'a@example.test', name: 'Initial Admin', role: 'admin', must_change_password: false })
        : jsonResponse(200, { permissions: ['*'] })
    ));
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');
    expect(auth.mustChangePassword).toBe(false);
    expect(fetch).toHaveBeenCalledWith('/api/users/me/permissions', expect.anything());
  });

  it('refreshes an access token that expired on the form before re-asking /me', async () => {
    const router = createAppRouter();
    await router.push('/profile');
    fetch.mockImplementation(async (url, opts) => {
      if (url === '/auth/refresh') return jsonResponse(200, { access_token: 'fresh-token' });
      if (url === '/api/users/me') {
        return opts?.headers?.Authorization === 'Bearer fresh-token'
          ? jsonResponse(200, { id: 'u1', role: 'admin', must_change_password: false })
          : jsonResponse(401, { detail: 'expired' });
      }
      return jsonResponse(200, { permissions: ['*'] });
    });
    await router.push('/customers');
    expect(fetch).toHaveBeenCalledWith('/auth/refresh', expect.anything());
    expect(router.currentRoute.value.path).toBe('/customers');
  });

  it('sends a held tab whose refresh has expired too to /login, not on to the page', async () => {
    const router = createAppRouter();
    await router.push('/profile');
    fetch.mockImplementation(async () => jsonResponse(401, { detail: 'expired' }));
    await router.push('/customers');
    expect(auth.isAuthenticated).toBe(false);
    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query.redirect).toBe('/customers');
  });

  it('still holds the tab when the server says the flag is set', async () => {
    const router = createAppRouter();
    await router.push('/profile');
    fetch.mockImplementation(async () => jsonResponse(200, { id: 'u1', email: 'a@example.test', name: 'Initial Admin', role: 'admin', must_change_password: true }));
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/profile');
    expect(fetch).toHaveBeenCalledWith('/api/users/me', expect.anything());
  });

  it('a 403 that lands after the change does not strand the person', async () => {
    const router = createAppRouter();
    await router.push('/profile');
    fetch.mockImplementation(async () => jsonResponse(200, { ok: 'password_changed' }));
    await createApiClient().post('/api/users/me/change-password', { current_password: 'a', new_password: 'b' });
    expect(auth.mustChangePassword).toBe(false);
    // A shell poll checked before the commit and answers after it.
    fetch.mockImplementation(async () => jsonResponse(403, { detail: { code: 'password_change_required', message: 'x' } }));
    await createApiClient().get('/api/notifications/unread').catch(() => {});
    expect(auth.mustChangePassword).toBe(true);
    fetch.mockImplementation(async (url) => (
      url === '/api/users/me'
        ? jsonResponse(200, { id: 'u1', email: 'a@example.test', role: 'admin', must_change_password: false })
        : jsonResponse(200, { permissions: ['*'] })
    ));
    await router.push('/customers');
    expect(router.currentRoute.value.path).toBe('/customers');
  });

  it('takes a set flag from GET /api/users/me, and leaves it alone when absent', async () => {
    auth.user = { name: 'Office', role: 'admin', must_change_password: false };
    fetch.mockImplementation(async () => jsonResponse(200, { id: 'u1', role: 'admin', must_change_password: true }));
    await createApiClient().get('/api/users/me');
    expect(auth.mustChangePassword).toBe(true);
    fetch.mockImplementation(async () => jsonResponse(200, { id: 'u1', role: 'admin' }));
    await createApiClient().get('/api/users/me');
    expect(auth.mustChangePassword).toBe(true);
  });

  it('keeps the flag through hydrateUser when /api/users/me omits it', async () => {
    fetch.mockImplementation(async () => jsonResponse(200, { id: 'u1', email: 'a@example.test', name: 'Initial Admin', role: 'admin' }));
    await auth.hydrateUser({ force: true });
    expect(auth.user.email).toBe('a@example.test');
    expect(auth.mustChangePassword).toBe(true);
  });

  it('sets the flag from the login response', async () => {
    auth.logout();
    fetch.mockImplementation(async (url) => (
      url === '/auth/login'
        ? jsonResponse(200, { access_token: 'tok', user: { id: 'u1', email: 'a@example.test', role: 'admin', must_change_password: true } })
        : jsonResponse(200, { permissions: [] })
    ));
    await auth.login({ email: 'a@example.test', password: 'bootstrap' });
    expect(auth.mustChangePassword).toBe(true);
    const router = createAppRouter();
    await router.push('/dashboard');
    expect(router.currentRoute.value.path).toBe('/profile');
  });
});
