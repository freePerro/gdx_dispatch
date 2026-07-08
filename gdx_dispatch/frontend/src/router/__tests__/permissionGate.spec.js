/**
 * Route permission gate — 2026-07-07 prod audit.
 *
 * The old gate fire-and-forgot loadPermissions() on a cold load and let
 * the navigation through, so a bookmarked /billing mounted for a user
 * without invoices.read_all and every data fetch 403-toasted (captured
 * live by the client-error tracker). Pins:
 *   1. Cold load + missing permission → redirected to /access-denied
 *      (page never mounts), with the path + permission in the query.
 *   2. Cold load + granted permission → navigation proceeds.
 *   3. Permission fetch failure → fail OPEN (backend is the enforcer;
 *      a network blip must not bounce a legitimate user).
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createAppRouter } from '../index';
import { useAuthStore } from '../../stores/auth';

function stubPermissionsFetch({ permissions = [], ok = true } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok,
      json: async () => ({ permissions }),
    })),
  );
}

async function navigate(path) {
  const router = createAppRouter();
  await router.push(path);
  return router.currentRoute.value;
}

describe('route permission gate', () => {
  let auth;

  beforeEach(() => {
    sessionStorage.clear();
    setActivePinia(createPinia());
    auth = useAuthStore();
    // Opaque token (no JWT payload) → role '' → not admin, not technician:
    // exercises the plain permission path with no escape hatches.
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office User', role: 'dispatcher' };
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('redirects a cold-load navigation without the permission to /access-denied', async () => {
    stubPermissionsFetch({ permissions: ['jobs.read_all'] });
    const current = await navigate('/billing');
    expect(current.path).toBe('/access-denied');
    expect(current.query.path).toBe('/billing');
    expect(current.query.permission).toBe('invoices.read_all');
  });

  it('lets a cold-load navigation through when the permission is granted', async () => {
    stubPermissionsFetch({ permissions: ['invoices.read_all'] });
    const current = await navigate('/billing');
    expect(current.name).toBe('billing');
  });

  it('fails open when the permission fetch fails (backend enforces)', async () => {
    stubPermissionsFetch({ ok: false });
    const current = await navigate('/billing');
    expect(current.name).toBe('billing');
  });

  it('leaves ungated routes alone', async () => {
    stubPermissionsFetch({ permissions: [] });
    const current = await navigate('/access-denied');
    expect(current.name).toBe('access-denied');
  });
});
