/**
 * The Payroll screen stops pretending.
 *
 * All three endpoints it called — `pay-periods`, `pay-stubs` and
 * `run-current-period` — are ui_compat 501 stubs, so every control failed and
 * the empty state told the operator to press the button that caused it.
 *
 * This does NOT build payroll. It asserts the screen is honest about not
 * having it, and that it no longer asks the server three times to confirm.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

test('payroll says it is not built instead of 501-ing on every control', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: access_token, tid: TENANT });

  // Watch for the requests that used to fire and fail on every visit.
  const deadCalls = [];
  page.on('request', (req) => {
    const u = req.url();
    if (/\/api\/payroll\/(pay-periods|pay-stubs|run-current-period)/.test(u)) deadCalls.push(u);
  });

  await page.goto('/payroll');
  const notice = page.getByTestId('payroll-not-built-notice');
  await expect(notice).toBeVisible({ timeout: 15000 });
  await expect(notice).toContainText('Payroll runs are not built');
  // It names the consequence and what still works, not just "unavailable".
  await expect(notice).toContainText('not implemented');
  await expect(notice).toContainText('Hours are unaffected');

  // The button that could only 501 is GONE, not merely disabled — the theme
  // renders a disabled primary button almost identically to an enabled one,
  // so it invited a click that silently did nothing.
  await expect(page.getByTestId('run-payroll-btn')).toHaveCount(0);

  await page.waitForTimeout(1500);
  expect(deadCalls, `the screen still called dead endpoints: ${deadCalls.join(', ')}`).toEqual([]);

  await page.screenshot({ path: 'test-results/payroll-honest-light.png' });

  // Dark mode: the notice is a colour-carrying panel.
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto('/payroll');
  await expect(page.getByTestId('payroll-not-built-notice')).toBeVisible({ timeout: 15000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await page.screenshot({ path: 'test-results/payroll-honest-dark.png' });
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));

  await api.dispose();
});
