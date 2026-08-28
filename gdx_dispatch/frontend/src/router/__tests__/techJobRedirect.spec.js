/**
 * Tech-role redirect covers a single job, not just the list.
 *
 * 2026-08-28 field report: a tech reached /jobs/:id from the mobile customer
 * page and got the desktop job page — labor/costing tabs 403'd in the
 * background and the photo button never uploaded. /jobs already redirected
 * to /mobile/jobs; /jobs/:id did not.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createAppRouter } from '../index';
import { useAuthStore } from '../../stores/auth';

function stubPermissionsFetch(permissions = []) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ permissions }) })));
}

async function navigate(path) {
  const router = createAppRouter();
  await router.push(path);
  return router.currentRoute.value;
}

describe('technician /jobs/:id redirect', () => {
  let auth;
  beforeEach(() => {
    sessionStorage.clear();
    setActivePinia(createPinia());
    auth = useAuthStore();
    auth.accessToken = 'opaque-test-token';
    stubPermissionsFetch(['jobs.read', 'jobs.write']);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('sends a technician on /jobs/:id to /mobile/jobs/:id, keeping the query', async () => {
    auth.user = { name: 'Tech', role: 'technician' };
    const current = await navigate('/jobs/7860adb1?tab=photos');
    expect(current.path).toBe('/mobile/jobs/7860adb1');
    expect(current.query.tab).toBe('photos');
  });

  it('accepts the short legacy spelling of the role', async () => {
    auth.user = { name: 'Tech', role: 'tech' };
    const current = await navigate('/jobs/abc');
    expect(current.path).toBe('/mobile/jobs/abc');
  });

  it('redirects on the JWT role claim alone when no user snapshot is cached', async () => {
    // Cold load: token in sessionStorage, gdx_user absent. The guard must
    // not wait for /api/users/me.
    const payload = btoa(JSON.stringify({ sub: 'u1', role: 'technician' })).replace(/=+$/, '');
    auth.accessToken = `hdr.${payload}.sig`;
    auth.user = null;
    const current = await navigate('/jobs/abc');
    expect(current.path).toBe('/mobile/jobs/abc');
  });

  it('leaves an office user on the desktop job page', async () => {
    auth.user = { name: 'Office', role: 'dispatcher' };
    const current = await navigate('/jobs/abc');
    expect(current.path).toBe('/jobs/abc');
  });
});
