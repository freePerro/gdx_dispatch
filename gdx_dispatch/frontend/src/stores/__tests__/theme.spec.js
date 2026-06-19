/**
 * Theme store tests — pins the S110b empty-Authorization defensive fix.
 *
 * Pre-fix, theme.js sent `Authorization: ''` (literal empty string) when no
 * token was in sessionStorage, which Postgres-side could interpret as a
 * malformed Bearer header → 401, then a stale-cache flash on the SPA. The
 * fix builds the headers object conditionally — Authorization is OMITTED
 * when the token is missing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useThemeStore } from '../theme';

describe('theme store loadBranding() Authorization header', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ company_name: 'Test', primary_color: '#000', accent_color: '#fff' }),
    });
    global.fetch = fetchMock;
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it('OMITS Authorization header when no token in sessionStorage', async () => {
    sessionStorage.setItem('gdx_tenant_slug', 'gdx');
    const store = useThemeStore();
    await store.loadBranding();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/settings/branding');
    expect(opts.headers).not.toHaveProperty('Authorization');
    // Tenant header still required on every request.
    expect(opts.headers['x-tenant-id']).toBe('gdx');
  });

  it('INCLUDES Bearer Authorization when token present', async () => {
    sessionStorage.setItem('gdx_access_token', 'fake-jwt');
    sessionStorage.setItem('gdx_tenant_slug', 'gdx');
    const store = useThemeStore();
    await store.loadBranding();

    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.headers.Authorization).toBe('Bearer fake-jwt');
    expect(opts.headers['x-tenant-id']).toBe('gdx');
  });

  it('NEVER sends an empty-string Authorization header', async () => {
    // No token. Pre-fix code sent `Authorization: ''` which is the
    // literal regression to lock down.
    const store = useThemeStore();
    await store.loadBranding();
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.headers.Authorization).toBeUndefined();
    // And critically: the header object must NOT contain the key at all
    // (some servers allow empty strings, but we want it absent so the
    // SPA's intent is unambiguous).
    expect(Object.keys(opts.headers)).not.toContain('Authorization');
  });
});
