/**
 * GDXA-312 — the admin payroll row Delete asks before it deletes.
 *
 * vitest cannot prove this: useDestructiveConfirm auto-accepts when no
 * ConfirmationService is registered, which is every unit test. Here the real
 * AppLayout ConfirmDialog renders, so Cancel must leave the entry and send no
 * DELETE, and only the dialog's Delete may remove it.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

test('payroll entry delete is confirmed, cancel keeps it', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  const auth = { authorization: `Bearer ${access_token}`, 'x-tenant-id': TENANT };

  const tech = `e2e-gdxa312-${Date.now()}`;
  const created = await api.post('/api/payroll/entries', {
    headers: { ...auth, 'content-type': 'application/json' },
    data: {
      tech_user_id: tech,
      period_start: '2026-09-01T00:00:00',
      period_end: '2026-09-14T00:00:00',
      hours_paid: 80,
      gross_pay: 2400,
    },
  });
  expect(created.status()).toBe(201);

  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });

  const deletes = [];
  page.on('request', (req) => {
    if (req.method() === 'DELETE' && req.url().includes('/api/payroll/entries/')) deletes.push(req.url());
  });

  await page.goto('/admin/payroll');
  const row = page.locator('tr', { hasText: tech });
  await expect(row).toBeVisible({ timeout: 15000 });

  // Cancel: the dialog names the entry, and nothing is deleted.
  await row.getByRole('button', { name: 'Delete' }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Delete payroll entry?');
  await expect(dialog).toContainText(tech);
  await expect(dialog).toContainText('80 h');
  await page.waitForTimeout(500); // let the dialog finish fading in
  await page.screenshot({ path: 'test-results/payroll-delete-confirm-light.png' });
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  await page.waitForTimeout(800);
  expect(deletes).toEqual([]);
  await expect(row).toBeVisible();

  // Dark mode: the dialog is readable on the dark surface.
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto('/admin/payroll');
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.getByRole('button', { name: 'Delete' }).click();
  await expect(dialog).toBeVisible();
  await page.waitForTimeout(500); // let the dialog finish fading in
  await page.screenshot({ path: 'test-results/payroll-delete-confirm-dark.png' });

  // Accept: exactly one DELETE, and the row leaves the table.
  await dialog.getByRole('button', { name: 'Delete' }).click();
  await expect(row).toHaveCount(0, { timeout: 10000 });
  expect(deletes).toHaveLength(1);

  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));
  await api.dispose();
});
