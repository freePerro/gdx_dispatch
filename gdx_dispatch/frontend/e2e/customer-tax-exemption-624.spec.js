/**
 * Customer tax exemption — browser proof (GDXA-624).
 *
 * POST/DELETE /api/tax/exemptions had no UI caller: /api/tax/resolve read the
 * rows to zero an exempt customer's invoice tax, and nobody could create one
 * from the app. This walks the card on CustomerDetail end to end, and checks
 * the rate the invoice flow would seed moves with it.
 *
 * The tax router admits admin/owner only (`_require_admin`). The office tier
 * (dispatcher) must see no card and get no 403 toast.
 *
 * Env: the usual E2E_* (global-setup's admin), plus E2E_DISPATCH_EMAIL /
 * E2E_DISPATCH_PASSWORD for the dispatcher check (skipped when unset).
 * SHOT_DIR, when set, receives light and dark screenshots.
 */
import fs from 'node:fs';
import path from 'node:path';
import { request as pwRequest } from '@playwright/test';
import { test as base, expect } from './_fixtures.js';

const test = base.extend({
  api: async ({ baseURL }, use) => {
    const fx = JSON.parse(fs.readFileSync(path.resolve('e2e/.state/fixtures.json'), 'utf8'));
    const ctx = await pwRequest.newContext({
      baseURL,
      extraHTTPHeaders: { authorization: `Bearer ${fx.token}`, 'content-type': 'application/json' },
    });
    await use(ctx);
    await ctx.dispose();
  },
});

const norm = (v) => String(v || '').replace(/-/g, '').toLowerCase();

async function clearExemptions(api, customerId) {
  const r = await api.get('/api/tax/exemptions');
  expect(r.ok()).toBeTruthy();
  for (const e of await r.json()) {
    if (norm(e.customer_id) === norm(customerId)) {
      const d = await api.delete(`/api/tax/exemptions/${e.id}`);
      expect(d.status()).toBe(204);
    }
  }
}

async function rate(api, customerId) {
  const r = await api.get(`/api/tax/resolve?customer_id=${customerId}`);
  expect(r.ok()).toBeTruthy();
  return (await r.json()).rate;
}

function shot(page, name) {
  const dir = process.env.SHOT_DIR;
  return dir ? page.screenshot({ path: path.join(dir, name), fullPage: false }) : null;
}

test('admin marks a customer tax-exempt and removes it, from the customer page', async ({ page, api, paramIds }) => {
  const cid = paramIds.customer;
  await clearExemptions(api, cid);
  // A fresh install's default rate is 0, which would make "exempt → rate 0"
  // pass with or without the row. Set a real rate for the walk, restore after.
  const before = await (await api.get('/api/tax/config')).json();
  const set = await api.patch('/api/tax/config', { data: { default_rate: 0.05 } });
  expect(set.ok()).toBeTruthy();
  try {
  expect(await rate(api, cid)).toBeGreaterThan(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(() => { if (window === window.top) localStorage.setItem('gdx_theme', 'light'); });
  await page.goto(`/customers/${cid}`);

  const card = page.locator('[data-testid="customer-tax-exemption-card"]');
  await expect(card).toBeVisible({ timeout: 20000 });
  await expect(page.locator('[data-testid="tax-exemption-status"]')).toHaveText('Taxable');

  // ── add ───────────────────────────────────────────────────────────────
  await page.locator('[data-testid="add-tax-exemption-btn"]').click();
  await expect(page.locator('[data-testid="tax-exemption-dialog"]')).toBeVisible();
  await page.locator('[data-testid="tax-ex-reason"]').fill('Reseller');
  await page.locator('[data-testid="tax-ex-cert"]').fill('CERT-624');
  await page.locator('[data-testid="save-tax-exemption-btn"]').click();
  await expect(page.locator('[data-testid="tax-exemption-dialog"]')).toBeHidden();
  await expect(page.locator('[data-testid="tax-exemption-status"]')).toHaveText('Tax-exempt');
  await expect(card).toContainText('CERT-624');
  await expect(card).toContainText('Reseller');
  await expect(page.locator('[data-testid="add-tax-exemption-btn"]')).toHaveCount(0);
  // The row the card wrote is the one the invoice flow reads.
  expect(await rate(api, cid)).toBe(0);
  await shot(page, 'tax-exemption-light.png');

  // ── dark mode, same state ───────────────────────────────────────────────
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.addInitScript(() => { if (window === window.top) localStorage.setItem('gdx_theme', 'dark'); });
  await page.reload();
  await expect(page.locator('[data-testid="tax-exemption-status"]')).toHaveText('Tax-exempt', { timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await shot(page, 'tax-exemption-dark.png');

  // ── remove: asks first, says permanent ──────────────────────────────────
  await page.locator('[data-testid^="remove-tax-exemption-"]').click();
  const dlg = page.locator('.p-confirmdialog');
  await expect(dlg).toBeVisible();
  await expect(dlg).toContainText(/permanent/i);
  await expect(dlg).toContainText(/audit log/i);
  await shot(page, 'tax-exemption-remove-confirm-dark.png');
  await dlg.getByRole('button', { name: 'Remove' }).click();
  await expect(page.locator('[data-testid="tax-exemption-status"]')).toHaveText('Taxable');
  await expect(page.locator('[data-testid="tax-exemption-none"]')).toBeVisible();
  expect(await rate(api, cid)).toBeGreaterThan(0);
  } finally {
    await api.patch('/api/tax/config', { data: { default_rate: before.default_rate } });
  }
});

test('the office tier (dispatcher) sees no tax card and no permission toast', async ({ browser, baseURL, paramIds }) => {
  const email = process.env.E2E_DISPATCH_EMAIL;
  const password = process.env.E2E_DISPATCH_PASSWORD;
  test.skip(!email || !password, 'E2E_DISPATCH_EMAIL / E2E_DISPATCH_PASSWORD not set');
  const ctx = await pwRequest.newContext({ baseURL });
  const r = await ctx.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': process.env.E2E_TENANT_SLUG, 'x-e2e-test': 'true' },
    data: { email, password },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await ctx.dispose();

  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.addInitScript((t) => { if (window !== window.top) return; sessionStorage.setItem('gdx_access_token', t); }, access_token);
  const taxCalls = [];
  page.on('request', (req) => { if (req.url().includes('/api/tax/exemptions')) taxCalls.push(req.url()); });
  await page.goto(`/customers/${paramIds.customer}`);
  await expect(page.locator('[data-testid="customer-info-card"]')).toBeVisible({ timeout: 20000 });
  await page.waitForTimeout(1000);
  await expect(page.locator('[data-testid="customer-tax-exemption-card"]')).toHaveCount(0);
  await expect(page.getByText(/permission denied/i)).toHaveCount(0);
  expect(taxCalls).toEqual([]);
  await context.close();
});
