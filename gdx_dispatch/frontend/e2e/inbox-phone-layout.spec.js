// GDXA-1753 — the office inbox at phone width. The three-pane grid
// (240px + minmax(280px, 1fr) + 2fr) measured ~800px on a 390px phone; at
// phone width it must show one pane at a time with no horizontal overflow,
// and desktop must keep all three panes side by side.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

async function primeSession(page, baseURL, theme) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await api.dispose();
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: access_token, tid: TENANT, theme });
}

// A view that never mounts leaves <main> empty and the first wait times out
// with "element(s) not found"; the page's own errors say why.
const pageErrors = [];
test.beforeEach(({ page }) => {
  pageErrors.length = 0;
  page.on('pageerror', (e) => pageErrors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') pageErrors.push(`console: ${m.text()}`); });
  page.on('requestfailed', (r) => pageErrors.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
});
test.afterEach(({}, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus) console.log(pageErrors.join('\n'));
});

async function overflowX(page) {
  return page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
}

for (const theme of ['light', 'dark']) {
  test.describe(`phone 390px, ${theme}`, () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

    test('one pane at a time, no horizontal overflow', async ({ page, baseURL }) => {
      await primeSession(page, baseURL, theme);
      await page.goto('/inbox');
      expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
      expect(new URL(page.url()).pathname).toBe('/inbox');

      const list = page.locator('[data-test="inbox-list"]');
      const rail = page.locator('[data-test="folder-rail"]');
      const toggle = page.locator('[data-test="inbox-folders-toggle"]');
      await expect(page.locator('[data-test="inbox-row"]').first()).toBeVisible({ timeout: 20000 });
      await expect(list).toBeVisible();
      await expect(rail).toBeHidden();
      await expect(toggle).toBeVisible();
      // The list gets the page, not a sliver of it.
      const listH = await list.evaluate((el) => el.getBoundingClientRect().height);
      expect(listH).toBeGreaterThan(400);
      expect(await overflowX(page)).toBeLessThanOrEqual(0);
      await page.screenshot({ path: `${SHOTS}/inbox-390-list-${theme}.png` });

      // Folders behind the toggle; picking one returns to the list.
      await toggle.click();
      await expect(rail).toBeVisible();
      await expect(list).toBeHidden();
      expect(await overflowX(page)).toBeLessThanOrEqual(0);
      await page.screenshot({ path: `${SHOTS}/inbox-390-folders-${theme}.png` });
      await page.locator('[data-test="folder-row"]').first().click();
      await expect(rail).toBeHidden();
      await expect(list).toBeVisible();

      // A row's ⋯ Move opens the dialog over the list, not a pane built
      // from row data.
      await page.locator('[data-test="msg-menu-trigger"]').first().click();
      await page.getByRole('menuitem', { name: 'Move to folder…' }).click();
      await expect(page.getByRole('dialog', { name: 'Move to folder' })).toBeVisible();
      await expect(page.locator('[data-test="inbox-detail"]')).toHaveCount(0);
      await page.getByRole('dialog', { name: 'Move to folder' }).getByRole('button', { name: 'Cancel' }).click();
      await expect(list).toBeVisible();

      // The reader keeps their place: open a row below the fold, come back.
      const scroller = page.locator('main.layout-content');
      await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; });
      const before = await scroller.evaluate((el) => el.scrollTop);
      expect(before).toBeGreaterThan(100);
      await page.locator('[data-test="inbox-row"]').last().click();
      await expect(page.locator('[data-test="inbox-detail"] .pane-header')).toBeInViewport({ timeout: 20000 });
      await page.locator('[data-test="inbox-detail"] .pane-header .btn-link').click();
      await expect(list).toBeVisible();
      await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBe(before);
      await scroller.evaluate((el) => { el.scrollTop = 0; });

      // list -> message -> back.
      await expect(page.locator('[data-test="inbox-row"]').first()).toBeVisible({ timeout: 20000 });
      await page.locator('[data-test="inbox-row"]').first().click();
      const detail = page.locator('[data-test="inbox-detail"]');
      await expect(detail).toBeVisible({ timeout: 20000 });
      await expect(list).toBeHidden();
      // Visible is not reachable: toBeVisible passed while Reply sat clipped
      // inside a 146px-tall overflow:hidden pane. In-viewport after a scroll
      // is the check that a thumb can get to it.
      const reply = page.locator('[data-test="inbox-reply"]');
      await reply.scrollIntoViewIfNeeded();
      await expect(reply).toBeInViewport();
      const frameH = await detail.locator('iframe').first().evaluate((f) => f.getBoundingClientRect().height);
      expect(frameH).toBeGreaterThanOrEqual(300);
      expect(await overflowX(page)).toBeLessThanOrEqual(0);
      await page.screenshot({ path: `${SHOTS}/inbox-390-detail-${theme}.png` });
      await detail.locator('.pane-header .btn-link').click();
      await expect(detail).toBeHidden();
      await expect(list).toBeVisible();

      // New -> compose pane, Cancel -> list.
      await page.locator('[data-test="inbox-new"]').click();
      await expect(page.locator('[data-test="inbox-compose"]')).toBeVisible();
      await expect(list).toBeHidden();
      const send = page.locator('[data-test="compose-send"]');
      await send.scrollIntoViewIfNeeded();
      await expect(send).toBeInViewport();
      expect(await overflowX(page)).toBeLessThanOrEqual(0);
      await page.screenshot({ path: `${SHOTS}/inbox-390-compose-${theme}.png` });
      await page.locator('[data-test="inbox-compose"] .pane-header .btn-link').click();
      await expect(list).toBeVisible();
    });
  });

  test.describe(`desktop 1440px, ${theme}`, () => {
    test.use({ viewport: { width: 1440, height: 900 } });

    test('three panes side by side, no Folders toggle', async ({ page, baseURL }) => {
      await primeSession(page, baseURL, theme);
      await page.goto('/inbox');
      await expect(page.locator('[data-test="inbox-row"]').first()).toBeVisible({ timeout: 20000 });
      await expect(page.locator('[data-test="folder-rail"]')).toBeVisible();
      await expect(page.locator('[data-test="inbox-list"]')).toBeVisible();
      await expect(page.locator('[data-test="inbox-folders-toggle"]')).toBeHidden();
      await page.locator('[data-test="inbox-row"]').first().click();
      await expect(page.locator('[data-test="inbox-detail"]')).toBeVisible({ timeout: 20000 });
      await expect(page.locator('[data-test="inbox-list"]')).toBeVisible();
      await expect(page.locator('[data-test="folder-rail"]')).toBeVisible();
      await page.screenshot({ path: `${SHOTS}/inbox-1440-detail-${theme}.png` });
    });
  });
}
