/**
 * Absence guard for the single-tenant residue purge, frontend half (S11 + S15).
 *
 * Until 2026-09-03 every request from the SPA carried an `x-tenant-id` header
 * derived from the hostname or a `gdx_tenant_slug` session key — residue of a
 * multi-tenant resolver the backend deleted months earlier. The help panel
 * also shipped a "Billing & subscription" article for a plan that was never
 * sold ("Starter, Pro, or Enterprise", "what you pay us, monthly").
 *
 * Counterfactual: restoring the header derivation in useApi, or re-adding the
 * article, turns the matching assertion red.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const HERE = dirname(fileURLToPath(import.meta.url));
const ARTICLES = join(HERE, '..', 'help', 'articles');

describe('S11 — the SPA no longer sends a tenant header', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    sessionStorage.clear();
    sessionStorage.setItem('gdx_tenant_slug', 'forged'); // a stale key must be ignored
  });

  it('useApi sends no x-tenant-id even when a stale slug is in sessionStorage', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({}),
      text: async () => '{}',
    }));
    vi.stubGlobal('fetch', fetchMock);
    const { createApiClient } = await import('../composables/useApi');
    const api = createApiClient();
    await api.get('/api/anything');
    expect(fetchMock).toHaveBeenCalled();
    const [, opts] = fetchMock.mock.calls[0];
    const names = Object.keys(opts.headers || {}).map((h) => h.toLowerCase());
    expect(names).not.toContain('x-tenant-id');
    expect(names).not.toContain('x-tenant');
    vi.unstubAllGlobals();
  });

  it('the auth store no longer exposes a tenant slug', async () => {
    const { useAuthStore } = await import('../stores/auth');
    const store = useAuthStore();
    // `tenantSlug` WAS in the store's return block until 2026-09-03; the
    // private `_tenantHeader` never was, so it is not asserted on.
    expect(store.tenantSlug).toBeUndefined();
  });
});

describe('S15 — the help panel sells no subscription', () => {
  const files = readdirSync(ARTICLES).filter((f) => f.endsWith('.md'));

  it('has no billing-subscription article', () => {
    expect(files).not.toContain('billing-subscription.md');
  });

  it('no article mentions a plan tier, a trial, or "what you pay us"', () => {
    const offenders = [];
    for (const f of files) {
      const text = readFileSync(join(ARTICLES, f), 'utf8');
      for (const needle of ['what you pay us', 'Starter, Pro, or Enterprise', 'billing-subscription', 'Billing & subscription', 'until trial ends', 'Settings → Subscription']) {
        if (text.includes(needle)) offenders.push(`${f}: ${needle}`);
      }
      // The help drawer's search for "subscription" must find nothing at all.
      if (/subscri/i.test(text)) offenders.push(`${f}: matches /subscri/i`);
    }
    expect(offenders).toEqual([]);
  });
});
