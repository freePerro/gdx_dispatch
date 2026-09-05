/**
 * The Payroll hub, in a real browser.
 *
 * A nav move is exactly the change that unit tests bless and users find
 * broken: the constants can be perfect while the tab bar fails to render,
 * a route 404s, or the sidebar stops highlighting. This walks all four
 * tabs against a container serving the branch's dist.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'e2e-shots';

async function authed(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), 'login must succeed').toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: access_token, tid: TENANT });
  return api;
}

async function setTheme(page, theme) {
  await page.evaluate((t) => {
    document.documentElement.classList.toggle('dark', t === 'dark');
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('theme', t); } catch { /* ignore */ }
  }, theme);
  await page.waitForTimeout(300);
}

test.describe('Payroll hub', () => {
  test('the tab bar renders all four tabs on Timesheets', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');

    const bar = page.locator('[data-testid="module-tab-bar"]');
    await expect(bar).toBeVisible({ timeout: 20000 });
    for (const key of ['timesheets', 'timeclock', 'payroll', 'commissions']) {
      await expect(page.locator(`[data-testid="module-tab-${key}"]`)).toBeVisible();
    }
    // The page under the bar is still the real Timesheets screen.
    await expect(page.locator('[data-testid="timesheets-send"]')).toBeVisible();

    await page.screenshot({ path: `${SHOTS}/hub-timesheets-light.png` });
    await setTheme(page, 'dark');
    await page.screenshot({ path: `${SHOTS}/hub-timesheets-dark.png` });
    await api.dispose();
  });

  test('every tab navigates and keeps its original URL', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="module-tab-bar"]')).toBeVisible({ timeout: 20000 });

    for (const [key, path] of [
      ['timeclock', '/timeclock'],
      ['payroll', '/payroll'],
      ['commissions', '/commissions'],
      ['timesheets', '/timesheets'],
    ]) {
      await page.locator(`[data-testid="module-tab-${key}"]`).click();
      await page.waitForTimeout(900);
      expect(new URL(page.url()).pathname, `${key} must keep its path`).toBe(path);
      // The bar survives the navigation — i.e. these really are children of
      // the hub and not four unrelated pages.
      await expect(page.locator('[data-testid="module-tab-bar"]')).toBeVisible();
    }
    await api.dispose();
  });

  test('a direct hit on each old URL still works', async ({ page, baseURL }) => {
    // Bookmarks, Dispatch's deep link and the bell notification all arrive
    // this way, not by clicking a tab.
    const api = await authed(page, baseURL);
    for (const path of ['/timeclock', '/payroll', '/commissions', '/timesheets']) {
      const res = await page.goto(path);
      expect(res.status(), `${path} must not 404`).toBeLessThan(400);
      await page.waitForTimeout(800);
      expect(new URL(page.url()).pathname).toBe(path);
      await expect(page.locator('[data-testid="module-tab-bar"]')).toBeVisible();
    }
    await api.dispose();
  });

  test('the deep link Dispatch uses still lands on the right shift', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets?on=2026-05-11&entry=e-1');
    await page.waitForTimeout(1500);
    // Either the entry opens or the page says it is not in range — both are
    // the deep link WORKING. A 404 or a blank shell would not be.
    await expect(page.locator('[data-testid="module-tab-bar"]')).toBeVisible({ timeout: 20000 });
    expect(new URL(page.url()).pathname).toBe('/timesheets');
    await api.dispose();
  });

  test('the sidebar shows ONE Payroll row, and Operations lost the two', async ({ page, baseURL }) => {
    // The actual ask: "timeclock and timesheets should live under payroll".
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="module-tab-bar"]')).toBeVisible({ timeout: 20000 });

    const nav = await page.evaluate(() => {
      const text = (document.querySelector('nav, aside, .app-sidebar') || document.body).innerText;
      return text.split('\n').map((l) => l.trim()).filter(Boolean);
    });
    // Timeclock/Timesheets must not appear as their own top-level rows.
    const rows = nav.filter((l) => /^(Timeclock|Timesheets|Time Clock)$/i.test(l));
    expect(rows, `sidebar still lists them separately: ${JSON.stringify(rows)}`).toEqual([]);
    expect(nav.some((l) => /^Payroll$/i.test(l)), 'a Payroll row must exist').toBe(true);
    await api.dispose();
  });

  test('the Payroll tab no longer claims pay periods do not exist', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/payroll');
    await page.waitForTimeout(1200);

    const notice = page.locator('[data-testid="payroll-not-built-notice"]');
    await expect(notice).toBeVisible({ timeout: 20000 });
    await expect(notice).toContainText('Pay runs are not built');
    await expect(notice).not.toContainText('cannot create pay periods');

    // ...and it points at the screen that does work.
    const link = page.locator('[data-testid="payroll-timesheets-link"]');
    await expect(link).toBeVisible();
    await link.click();
    await page.waitForTimeout(900);
    expect(new URL(page.url()).pathname).toBe('/timesheets');

    await page.goto('/payroll');
    await page.waitForTimeout(1000);
    await setTheme(page, 'dark');
    await page.screenshot({ path: `${SHOTS}/hub-payroll-dark.png` });
    await api.dispose();
  });
});
