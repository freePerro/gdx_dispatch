import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

const EXPECTED = ['Reports', 'Variance Report', 'Forecasting', 'Budget', 'Spending Trends'];

test('Experimental nav group contains the relocated reporting items', async ({ page, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();

  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });

  await page.goto('/forecasting');
  await expect(page.locator('.sidebar, nav').first()).toBeVisible({ timeout: 15000 });

  // Expand the "Experimental" category header.
  const header = page.locator('.menu-group-header, .p-panelmenu-header', { hasText: 'Experimental' }).first();
  await expect(header).toBeVisible({ timeout: 10000 });
  await header.click();

  // All five relocated items should now be reachable as menu links.
  for (const label of EXPECTED) {
    const link = page.locator('.menu-item-link', { hasText: new RegExp(`^\\s*${label}\\s*$`) }).first();
    await expect(link, `expected "${label}" under Experimental`).toBeVisible({ timeout: 10000 });
  }

  // Screenshot the expanded group for visual proof.
  await header.scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/experimental-nav.png', fullPage: false });
  await api.dispose();
});
