/**
 * PR A — the job detail screen offers what the route card offers.
 * Walked at a phone viewport against a throwaway container serving local code.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const SECRET = process.env.E2E_PASSWORD;
const JOB = process.env.E2E_JOB_ID;

async function auth(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: SECRET },
  });
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: body.access_token, tid: TENANT });
  await api.dispose();
}

test.use({ viewport: { width: 375, height: 812 } });

test('the three new actions render and are thumb-sized', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.goto(`/mobile/jobs/${JOB}`);
  await expect(page.locator('[data-testid="mobile-job-detail-actions"]')).toBeVisible({ timeout: 20000 });

  for (const id of ['mjd-quote', 'mjd-change-order', 'mjd-chat']) {
    const el = page.locator(`[data-testid="${id}"]`);
    await expect(el, `${id} must render`).toBeVisible();
    const box = await el.boundingBox();
    expect(box, `${id} needs a box`).not.toBeNull();
    expect(box.height, `${id} height ${box.height} < 44px`).toBeGreaterThanOrEqual(44);
  }
  await page.screenshot({ path: 'test-results/pra-detail-light.png', fullPage: true });
});

test('every action is actually tappable — nothing covers it', async ({ page, baseURL }) => {
  // The assertion that a browser walk forced into existence. toBeVisible() is
  // true for a button sitting UNDER the sticky action bar or under the floating
  // "+" FAB. The first cut of PR A put three buttons in the sticky bar; it grew
  // to three rows, covered the equipment list, and the FAB sat squarely on top
  // of Chat. Every check was green. Only the screenshot showed it.
  await auth(page, baseURL);
  await page.goto(`/mobile/jobs/${JOB}`);
  await expect(page.locator('[data-testid="mjd-secondary-actions"]')).toBeVisible({ timeout: 20000 });

  const ids = ['mjd-quote', 'mjd-change-order', 'mjd-chat', 'mjd-complete', 'mjd-navigate'];
  const covered = [];
  for (const id of ids) {
    const loc = page.locator(`[data-testid="${id}"]`);
    if (!(await loc.count())) continue;
    // Scroll it to the middle first: below-the-fold is not a defect, being
    // covered once you HAVE scrolled to it is. The sticky bar and the FAB stay
    // put while the page moves, so this is exactly when occlusion shows up.
    await loc.scrollIntoViewIfNeeded();
    await page.waitForTimeout(150);
    const bad = await page.evaluate((testid) => {
      const el = document.querySelector(`[data-testid="${testid}"]`);
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return `${testid}: zero-size`;
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (cy < 0 || cy > window.innerHeight) return `${testid}: could not be scrolled into view`;
      const hit = document.elementFromPoint(cx, cy);
      if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) {
        const cls = hit && hit.className && typeof hit.className === 'string'
          ? '.' + hit.className.trim().split(/\s+/).join('.') : '';
        return `${testid}: covered by <${hit ? hit.tagName.toLowerCase() : 'null'}${cls}>`;
      }
      return null;
    }, id);
    if (bad) covered.push(bad);
  }
  expect(covered, `controls not reachable by tap: ${covered.join(' | ')}`).toEqual([]);
});

test('job context and the customer warning render', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.goto(`/mobile/jobs/${JOB}`);
  const ctx = page.locator('[data-testid="mjd-job-context"]');
  await expect(ctx).toBeVisible({ timeout: 20000 });
  await expect(ctx).toContainText('Emergency');
  await expect(page.locator('[data-testid="mjd-return-visit"]')).toBeVisible();
  await expect(ctx).toContainText('dog warning');
  await expect(page.locator('[data-testid="mjd-customer-notes"]')).toContainText('Beware of dog');
});

test('equipment is NOT fetched until the tech expands it', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  const equipCalls = [];
  page.on('request', (r) => { if (r.url().includes('/equipment')) equipCalls.push(r.url()); });

  await page.goto(`/mobile/jobs/${JOB}`);
  await expect(page.locator('[data-testid="mjd-equipment-toggle"]')).toBeVisible({ timeout: 20000 });
  await page.waitForTimeout(1200);
  expect(equipCalls, 'equipment must not be fetched at mount').toHaveLength(0);

  await page.locator('[data-testid="mjd-equipment-toggle"]').click();
  await expect(page.locator('[data-testid="mjd-equipment-list"]')).toBeVisible({ timeout: 15000 });
  expect(equipCalls.length, 'expanding must fetch exactly once').toBe(1);
  await expect(page.locator('[data-testid="mjd-equipment-list"]')).toContainText('CHI');
  await expect(page.locator('[data-testid="mjd-equipment-list"]')).toContainText('LiftMaster');

  // Collapse + re-expand must not refetch — it is cached.
  await page.locator('[data-testid="mjd-equipment-toggle"]').click();
  await page.locator('[data-testid="mjd-equipment-toggle"]').click();
  await page.waitForTimeout(600);
  expect(equipCalls.length, 're-expanding must reuse the cache').toBe(1);
});

test('dark mode: the new sections are readable', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto(`/mobile/jobs/${JOB}`);
  await expect(page.locator('[data-testid="mjd-job-context"]')).toBeVisible({ timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await page.locator('[data-testid="mjd-equipment-toggle"]').click();
  await expect(page.locator('[data-testid="mjd-equipment-list"]')).toBeVisible({ timeout: 15000 });

  // Contrast is a real risk here: .customer-notes sets its own background.
  const contrast = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="mjd-customer-notes"]');
    if (!el) return null;
    const s = getComputedStyle(el);
    const lum = (c) => {
      const [r, g, b] = c.match(/\d+/g).slice(0, 3).map(Number).map((v) => {
        const x = v / 255;
        return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    let bg = s.backgroundColor;
    let node = el;
    while (bg === 'rgba(0, 0, 0, 0)' && node.parentElement) {
      node = node.parentElement;
      bg = getComputedStyle(node).backgroundColor;
    }
    const L1 = lum(s.color), L2 = lum(bg);
    return (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
  });
  expect(contrast, 'customer warning note contrast in dark mode').not.toBeNull();
  expect(contrast).toBeGreaterThanOrEqual(4.5);
  await page.screenshot({ path: 'test-results/pra-detail-dark.png', fullPage: true });
});

test("Today's Route still shows its heading — PR A removed nothing", async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.goto('/mobile');
  await page.waitForTimeout(2500);
  const body = await page.locator('body').innerText();
  expect(body).toContain("Today's Route");
  await page.screenshot({ path: 'test-results/pra-today-light.png', fullPage: true });
});
