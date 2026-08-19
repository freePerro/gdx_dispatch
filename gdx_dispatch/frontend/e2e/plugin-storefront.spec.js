import { expect, request as pwRequest, test } from '@playwright/test';

/**
 * The plugin storefront, walked as the owner actually walks it.
 *
 * The two things a unit test cannot check are exactly the two that matter
 * here: that the permissions a plugin will ask for are visible BEFORE the
 * install button does anything, and that the confirmation is a real modal
 * rather than `useDestructiveConfirm` (which auto-accepts silently, issue
 * #215 — on this screen that would make Install unconfirmed code execution).
 */

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PW = process.env.E2E_PASSWORD;

async function signIn(page, baseURL, { dark = false } = {}) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PW },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    if (a.dark) localStorage.setItem('gdx_theme', 'dark');
  }, { t: access_token, tid: TENANT, dark });
  await api.dispose();
}

async function openStore(page) {
  await page.goto('/admin/plugins');
  await expect(page.getByText('Browse plugins')).toBeVisible({ timeout: 20000 });
  const grid = page.getByTestId('store-grid');
  await expect(grid).toBeVisible({ timeout: 20000 });
  return grid;
}

test('the catalog renders a card per plugin', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  const grid = await openStore(page);

  // The live catalog publishes these three.
  for (const key of ['example', 'hvac', 'n8n']) {
    await expect(grid.locator(`[data-plugin="${key}"]`)).toBeVisible();
  }
  await expect(grid.locator('[data-plugin="n8n"]')).toContainText('n8n Automations');
});

test('permissions are visible before install, not after', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  const grid = await openStore(page);

  // n8n asks for `events`; the card must say so without anything being clicked.
  const n8n = grid.locator('[data-plugin="n8n"]');
  await expect(n8n).toContainText('events');
  await expect(n8n).toContainText('asks for elevated access');

  // A plugin that asks for nothing must say that too, rather than staying blank.
  await expect(grid.locator('[data-plugin="hvac"]')).toContainText('no elevated permissions');
});

test('Install opens a real confirmation modal that can be cancelled', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  const grid = await openStore(page);

  const card = grid.locator('[data-plugin="hvac"]');
  await card.getByRole('button', { name: 'Install' }).click();

  // A real dialog with explicit buttons — NOT useDestructiveConfirm, which
  // resolves true without rendering anything.
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('HVAC Catalog Pack');
  await expect(dialog).toContainText("Installing runs the plugin's code");
  await expect(page.getByTestId('confirm-install')).toBeVisible();

  // Cancel must actually cancel: nothing installed, card unchanged.
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  await expect(card.getByRole('button', { name: 'Install' })).toBeVisible();
});

test('an installed plugin reads as running with a way into it', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  const grid = await openStore(page);

  // n8n was installed through this same store before the walk.
  const n8n = grid.locator('[data-plugin="n8n"]');
  await expect(n8n).toContainText('Running v');
  // No dead end — a loaded plugin offers a way to open it.
  await expect(n8n.getByRole('button', { name: 'Open' })).toBeVisible();
});

test('the storefront is readable in dark mode', async ({ page, baseURL }) => {
  await signIn(page, baseURL, { dark: true });
  const grid = await openStore(page);
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  // The card must not be painted a light background in a dark theme — the
  // classic hardcoded-colour bug that jsdom can never catch.
  const card = grid.locator('[data-plugin="n8n"]');
  const colors = await card.evaluate((el) => {
    const s = getComputedStyle(el);
    const rgb = (v) => (v.match(/\d+/g) || []).slice(0, 3).map(Number);
    return { bg: rgb(s.backgroundColor), fg: rgb(s.color) };
  });
  const lum = (c) => (c.length === 3 ? (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) : null);
  expect(lum(colors.bg)).toBeLessThan(128);   // dark surface
  expect(lum(colors.fg)).toBeGreaterThan(128); // light text on it

  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/storefront-dark.png', fullPage: true });
});

test('the cards stack instead of overflowing on a phone', async ({ page, baseURL }) => {
  // jsdom applies no media queries, so column behaviour can only be proven in a
  // real browser. The failure this guards is a grid that forces a wider layout
  // than the screen and makes the page scroll sideways.
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, baseURL);
  const grid = await openStore(page);

  const overflows = await page.evaluate(() =>
    document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(overflows, 'the page scrolls sideways on a phone').toBe(false);

  // One column at this width — cards below each other, not squeezed side by side.
  const boxes = await grid.locator('.store-card').evaluateAll((els) =>
    els.map((e) => e.getBoundingClientRect().left));
  expect(new Set(boxes.map(Math.round)).size).toBe(1);

  await page.screenshot({ path: 'test-results/storefront-mobile.png', fullPage: true });
});

test('the storefront looks right in light mode', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  const grid = await openStore(page);
  await grid.locator('[data-plugin="n8n"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/storefront-light.png', fullPage: true });
});
