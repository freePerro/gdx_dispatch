/**
 * GDXA-413 — on the customer page, the location card's Set Primary switch
 * saves (it sent the DOM Event and got 422), and the Edit dialog's Customer
 * Type shows a Contractor customer as Contractor (it offered only two types).
 *
 * Self-seeding: creates its own customer and two locations through the API
 * and soft-deletes the customer afterwards.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

test('Set Primary saves and Customer Type keeps Contractor', async ({ page, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token: token } = await login.json();
  const headers = { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT, 'content-type': 'application/json' };

  const cust = await (await api.post('/api/customers', {
    headers, data: { name: `GDXA-413 walk ${Date.now()}`, customer_type: 'contractor' },
  })).json();
  const locA = await (await api.post(`/api/customers/${cust.id}/locations`, {
    headers, data: { label: 'Yard', address: '1 Walk St' },
  })).json();
  const locB = await (await api.post(`/api/customers/${cust.id}/locations`, {
    headers, data: { label: 'Shop', address: '2 Walk Ave' },
  })).json();

  try {
    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    }, { t: token, tid: TENANT, theme: process.env.E2E_THEME || 'light' });

    await page.goto(`/customers/${cust.id}`);
    await page.locator('[data-testid="tab-locations"]').click();
    const toggle = page.locator(`[data-testid="primary-toggle-${locB.id}"]`);
    await expect(toggle).toBeVisible({ timeout: 15000 });

    const patched = page.waitForResponse((r) =>
      r.request().method() === 'PATCH' && r.url().includes(`/locations/${locB.id}`));
    await toggle.click();
    const resp = await patched;
    expect(resp.request().postDataJSON()).toEqual({ is_primary: true });
    expect(resp.status()).toBe(200);

    // The card stays on after the refetch, and the server agrees.
    await expect(toggle.locator('input')).toBeChecked();
    const locs = await (await api.get(`/api/customers/${cust.id}/locations`, { headers })).json();
    expect(locs.find((l) => l.id === locB.id).is_primary).toBe(true);
    expect(locs.find((l) => l.id === locA.id).is_primary).toBe(false);
    await page.screenshot({ path: `${SHOTS}/413-locations-${process.env.E2E_THEME || 'light'}.png` });

    // Customer Type: the Contractor customer opens as Contractor.
    await page.locator('[data-testid="edit-customer-btn"]').click();
    const typeSelect = page.locator('[data-testid="edit-customer-type"]');
    await expect(typeSelect).toContainText('Contractor');
    await typeSelect.click();
    const options = page.locator('.p-select-overlay [role="option"]');
    await expect(options).toHaveText(['Residential', 'Commercial', 'Retail', 'Contractor', 'Wholesale', 'Property Manager']);
    await page.screenshot({ path: `${SHOTS}/413-type-${process.env.E2E_THEME || 'light'}.png` });
  } finally {
    await api.delete(`/api/customers/${cust.id}`, { headers });
    await api.dispose();
  }
});
