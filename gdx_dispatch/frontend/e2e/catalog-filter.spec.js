// Browser verification of the Catalog active/inactive filter (#50 follow-up).
// Drives the real rendered SPA: primes the auth token in localStorage, opens
// /catalog, and checks the filter actually shows/hides catalog tabs.
//
// Requires a container serving a freshly-built dist (the .vue change must be in
// the bundle). Seeded catalog ids + creds come from env.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const ACTIVE_ID = process.env.E2E_CAT_ACTIVE || '';
const INACTIVE_ID = process.env.E2E_CAT_INACTIVE || '';

test('catalog active/inactive filter shows and hides tabs in the browser', async ({ page, baseURL }) => {
  test.skip(!EMAIL || !ACTIVE_ID, 'creds / seeded catalog ids not set');

  // Auth: mint a token via the API, prime it the way the SPA's auth store expects.
  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    // x-e2e-test bypasses the rate limiter when GDX_E2E_BYPASS=1 on the server.
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok(), `login ${login.status()}`).toBeTruthy();
  const { access_token } = await login.json();
  // The SPA's auth store reads sessionStorage 'gdx_access_token' / 'gdx_tenant_slug'.
  await page.addInitScript(([t, tid]) => {
    sessionStorage.setItem('gdx_access_token', t);
    sessionStorage.setItem('gdx_tenant_slug', tid);
  }, [access_token, TENANT]);

  await page.goto('/catalog');

  const filter = page.locator('[data-testid="catalog-active-filter"]');
  await expect(filter).toBeVisible({ timeout: 15000 });

  const activeTab = page.locator(`[data-testid="catalog-${ACTIVE_ID}"]`);
  const inactiveTab = page.locator(`[data-testid="catalog-${INACTIVE_ID}"]`);

  // Default = Active: the active catalog shows, the inactive one is hidden.
  await expect(activeTab).toBeVisible();
  await expect(inactiveTab).toHaveCount(0);

  // Toggle → All: both show.
  await filter.getByText('All', { exact: true }).click();
  await expect(activeTab).toBeVisible();
  await expect(inactiveTab).toBeVisible();

  // Toggle → Inactive: only the inactive one.
  await filter.getByText('Inactive', { exact: true }).click();
  await expect(inactiveTab).toBeVisible();
  await expect(activeTab).toHaveCount(0);

  await api.dispose();
});
