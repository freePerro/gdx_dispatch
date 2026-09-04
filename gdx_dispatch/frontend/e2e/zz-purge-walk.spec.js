// Browser walk of the single-tenant residue purge against a FRESH install
// (throwaway Postgres + the image entrypoint) serving this working tree.
//
// What a real person must see / not see after the purge:
//   - login → dashboard works as the bootstrapped owner
//   - Settings → Modules lists modules and a toggle round-trips through the API
//   - the help drawer's search for "subscription" finds nothing
//   - /superadmin, /legacy/dashboard, /integrations render the SPA, never a
//     vendor console
//   - NO request the SPA makes carries x-tenant-id / X-Tenant
//   - light + dark, desktop + mobile screenshots
//
// Run:  E2E_BASE_URL=http://127.0.0.1:8003 E2E_EMAIL=owner@example.com \
//       E2E_PASSWORD=$(cat ../../../scratch_e2e/pw_purge.txt) \
//       node_modules/.bin/playwright test e2e/zz-purge-walk.spec.js --retries=0
import { test, expect, request as pwRequest } from '@playwright/test';

const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const SHOTS = process.env.E2E_SHOT_DIR || '/home/doug/github_gdx_dispatch/scratch_e2e/purge-walk';

async function loginToken(baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), `login ${r.status()}`).toBeTruthy();
  const { access_token } = await r.json();
  await api.dispose();
  return access_token;
}

async function prime(page, token) {
  await page.addInitScript((t) => {
    sessionStorage.setItem('gdx_access_token', t);
  }, token);
}

function recordHeaders(page, seen) {
  page.on('request', (req) => {
    const names = Object.keys(req.headers()).map((h) => h.toLowerCase());
    for (const n of names) {
      if (n === 'x-tenant-id' || n === 'x-tenant') seen.push(`${req.method()} ${req.url()} → ${n}`);
    }
  });
}

test.describe('purge walk on a fresh install', () => {
  test.setTimeout(120_000);

  test('owner logs in, sees the dashboard and settings modules; no tenant header ever leaves the browser', async ({ page, baseURL }) => {
    const token = await loginToken(baseURL);
    const offenders = [];
    recordHeaders(page, offenders);
    await prime(page, token);

    // Dashboard, light, desktop
    await page.setViewportSize({ width: 1366, height: 900 });
    await page.goto('/dashboard');
    await expect(page.locator('#app')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open help' })).toBeVisible({ timeout: 20_000 });
    await page.screenshot({ path: `${SHOTS}/dashboard-light-desktop.png`, fullPage: false });

    // Settings → Modules: the gate seeded every module on first check
    await page.goto('/settings');
    await page.getByRole('tab', { name: 'Modules' }).click();
    await expect(page.getByTestId('modules-list')).toBeVisible({ timeout: 20_000 });
    const toggles = page.locator('[data-testid^="module-toggle-"]');
    const count = await toggles.count();
    expect(count).toBeGreaterThan(10);
    // No SaaS "Upgrade" lock tag on any module
    await expect(page.getByTestId('module-locked-tag')).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/settings-modules-light-desktop.png` });

    // Toggle one non-core module off, save, verify via API, then restore.
    const key = 'games';
    const toggle = page.getByTestId(`module-toggle-${key}`);
    if (await toggle.count()) {
      await toggle.click();
      await expect(page.getByTestId('modules-dirty-hint')).toBeVisible();
      await page.getByTestId('modules-save-btn').click();
      await expect(page.getByTestId('modules-dirty-hint')).toHaveCount(0, { timeout: 15_000 });
      const api = await pwRequest.newContext({ baseURL, extraHTTPHeaders: { authorization: `Bearer ${token}` } });
      const mods = await (await api.get('/api/settings/modules')).json();
      const list = Array.isArray(mods) ? mods : (mods.modules || Object.values(mods));
      const games = list.find((m) => m.key === key || m.module_key === key);
      expect(games, 'games module present in API').toBeTruthy();
      expect(games.enabled).toBe(false);
      // restore
      await api.post(`/api/settings/modules/${key}/enable`).catch(() => {});
      await api.dispose();
    }

    // Help drawer: search "subscription" must find nothing
    await page.getByRole('button', { name: 'Open help' }).click();
    const search = page.getByRole('textbox', { name: 'Search help' });
    await expect(search).toBeVisible({ timeout: 10_000 });
    await search.fill('subscription');
    await page.waitForTimeout(600);
    await expect(page.locator('[data-test="help-result-link"]')).toHaveCount(0);
    await expect(page.getByText(/what you pay us|Starter, Pro, or Enterprise|Billing & subscription/)).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/help-search-subscription-light.png` });
    await search.fill('invoice');
    await page.waitForTimeout(600);
    expect(await page.locator('[data-test="help-result-link"]').count(), 'a real search still returns results').toBeGreaterThan(0);
    await page.keyboard.press('Escape');

    // Retired surfaces render the SPA, never a vendor console
    for (const path of ['/superadmin', '/legacy/dashboard', '/legacy/settings', '/integrations']) {
      await page.goto(path);
      await expect(page.locator('#app')).toBeVisible();
      await expect(page.getByText('Suspend Tenant')).toHaveCount(0);
      await expect(page.getByText('Impersonate Tenant')).toHaveCount(0);
    }
    await page.screenshot({ path: `${SHOTS}/superadmin-falls-through-light.png` });

    // Dark mode, desktop
    await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
    await page.goto('/dashboard');
    await expect(page.getByRole('button', { name: 'Open help' })).toBeVisible({ timeout: 20_000 });
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
    await page.screenshot({ path: `${SHOTS}/dashboard-dark-desktop.png` });
    await page.goto('/settings');
    await page.getByRole('tab', { name: 'Modules' }).click();
    await expect(page.getByTestId('modules-list')).toBeVisible({ timeout: 20_000 });
    await page.screenshot({ path: `${SHOTS}/settings-modules-dark-desktop.png` });
    await page.getByRole('button', { name: 'Open help' }).click();
    await page.getByRole('textbox', { name: 'Search help' }).fill('subscription');
    await page.waitForTimeout(600);
    await expect(page.locator('[data-test="help-result-link"]')).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/help-search-subscription-dark.png` });
    await page.keyboard.press('Escape');

    // Mobile, dark then light
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/dashboard');
    await expect(page.locator('#app')).toBeVisible();
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${SHOTS}/dashboard-dark-mobile.png` });
    await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));
    await page.goto('/dashboard');
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${SHOTS}/dashboard-light-mobile.png` });

    // The whole walk: not one request carried a tenant header.
    expect(offenders, 'requests that carried a tenant header').toEqual([]);
  });
});
