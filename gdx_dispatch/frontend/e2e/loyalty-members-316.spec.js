// GDXA-316: the Loyalty page reads the real points ledger.
//
// It used to read GET /api/loyalty, a ui_compat stub that always answered an
// empty list, and its dialog posted to /api/loyalty/adjust and /redeem, which
// never existed. This walk awards points through the dialog as office staff
// and checks the member row appears, reading GET /api/loyalty/members.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR;

async function login(baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  return { api, token: access_token };
}

async function prime(page, token, theme) {
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: token, tid: TENANT, theme });
}

test('award points through the dialog and see the member listed', async ({ page, baseURL }) => {
  const { api, token } = await login(baseURL);
  const auth = { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT };
  const customers = await (await api.get('/api/customers/search?q=a', { headers: auth })).json();
  expect(customers.length).toBeGreaterThan(0);
  const customer = customers[0];

  const requested = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.pathname.startsWith('/api/loyalty')) requested.push(`${req.method()} ${u.pathname}`);
  });

  await prime(page, token, 'light');
  await page.goto('/loyalty');
  await expect(page.locator('[data-testid="loyalty-members-table"]')).toBeVisible({ timeout: 15000 });

  await page.locator('[data-testid="loyalty-dialog-btn"]').click();
  const input = page.locator('[data-testid="loyalty-customer"] input, input[data-testid="loyalty-customer"]').first();
  await input.fill(customer.name.slice(0, 4));
  await page.locator('.p-autocomplete-option').filter({ hasText: customer.name }).first().click();
  await page.locator('[data-testid="loyalty-points"] input, input[data-testid="loyalty-points"]').first().fill('6000');
  await page.locator('[data-testid="loyalty-reason"]').fill('GDXA-316 browser walk');
  await page.locator('[data-testid="loyalty-save-btn"]').click();

  const row = page.locator('[data-testid="loyalty-members-table"] tr').filter({ hasText: customer.name });
  await expect(row).toBeVisible({ timeout: 10000 });
  await expect(row).toContainText('GOLD');
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/loyalty-light-desktop.png`, fullPage: true });

  expect(requested).toContain('GET /api/loyalty/members');
  expect(requested).toContain(`POST /api/loyalty/customers/${customer.id}/points`);
  expect(requested).not.toContain('GET /api/loyalty');
  expect(requested.some((r) => r.endsWith('/adjust') || r.endsWith('/redeem'))).toBe(false);

  // The member link opens that customer's record.
  await row.locator('[data-testid="loyalty-member-link"]').click();
  await expect(page).toHaveURL(new RegExp(`/customers/${customer.id}`));
  await api.dispose();
});

for (const [theme, width, height, label] of [
  ['dark', 1366, 900, 'dark-desktop'],
  ['dark', 390, 844, 'dark-mobile'],
  ['light', 390, 844, 'light-mobile'],
]) {
  test(`loyalty page renders members (${label})`, async ({ page, baseURL }) => {
    const { api, token } = await login(baseURL);
    await page.setViewportSize({ width, height });
    await prime(page, token, theme);
    await page.goto('/loyalty');
    await expect(page.locator('[data-testid="loyalty-members-table"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="loyalty-member-link"]').first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/loyalty-${label}.png`, fullPage: true });
    await api.dispose();
  });
}
