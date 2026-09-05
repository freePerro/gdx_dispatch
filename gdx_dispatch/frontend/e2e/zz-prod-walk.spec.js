/**
 * PROD WALK — v1.74.0, one job card.
 *
 * READ-ONLY BY CONSTRUCTION. This runs against gdx.teamgaragedoor.com with real
 * customer data and a real dispatch board, so it navigates and observes and
 * does nothing else. It never taps "On my way", "I'm here", "Complete",
 * "Bill / collect", "Change order", or the navigate button — advancing a real
 * job's status or messaging real dispatch is not a verification, it is an
 * incident. The only interaction is opening a card, which is a GET.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const BASE = process.env.WALK_BASE || 'https://gdx.teamgaragedoor.com';
const EMAIL = process.env.PROD_EMAIL;
const SECRET = process.env.PROD_PASSWORD;
const TENANT = process.env.PROD_TENANT;

// ONE login for the whole walk. A real deployment rate-limits /auth/login, and
// logging in per-test 429s everything after the first — which reads as a
// product failure and is not one.
let TOKEN = null;

async function auth(page) {
  if (!TOKEN) {
    const api = await pwRequest.newContext({ baseURL: BASE });
    const r = await api.post('/auth/login', {
      headers: { 'content-type': 'application/json', },
      data: { email: EMAIL, password: SECRET },
    });
    expect(r.ok(), `login failed: ${r.status()} (429 = rate limited, not a defect)`).toBeTruthy();
    const body = await r.json();
    expect(body.access_token, 'no access token returned').toBeTruthy();
    TOKEN = body.access_token;
    await api.dispose();
  }
  await page.addInitScript((t) => {
    sessionStorage.setItem('gdx_access_token', t);
  }, TOKEN);
}


/** Switch to company scope. The walk account owns no jobs itself, so "My jobs"
 *  is legitimately empty — that empty state is correct behaviour, not a bug.
 *  Changing the scope filter is a read-only GET. */
async function showAllJobs(page) {
  await page.goto(`${BASE}/mobile/jobs`);
  const all = page.getByText('All jobs', { exact: true }).first();
  if (await all.count()) {
    await all.click();
    await page.waitForTimeout(2500);
  }
}

test.use({ viewport: { width: 375, height: 812 } });

test('the Jobs list renders the shared card and it opens the job', async ({ page }) => {
  await auth(page);
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await showAllJobs(page);
  const card = page.locator('[data-testid^="mobile-job-card-"]').first();
  await expect(card, 'no shared job card rendered on prod').toBeVisible({ timeout: 30000 });

  // The card must be a real link — the whole point of the release.
  const href = await card.getAttribute('href');
  expect(href, 'the card is not a link').toMatch(/\/mobile\/jobs\/[0-9a-f-]{36}/);

  await card.click();
  await expect(page).toHaveURL(/\/mobile\/jobs\/[0-9a-f-]{36}/, { timeout: 20000 });
  await expect(page.locator('h1')).toContainText('Job details');
  expect(errors, `JS errors on prod: ${errors.join(' | ')}`).toEqual([]);
});

test("Today's Route renders without error and its cards are links", async ({ page }) => {
  await auth(page);
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${BASE}/mobile`);
  await page.waitForTimeout(4000);
  const body = await page.locator('body').innerText();

  // The crash this release nearly shipped rendered exactly this.
  expect(body, 'Today\'s Route is throwing on prod').not.toContain('We hit an error rendering this page');
  expect(body).toContain("Today's Route");
  expect(errors, `JS errors on prod: ${errors.join(' | ')}`).toEqual([]);

  // If this account has stops, every one of them must be a link.
  // `a[...]` matters: the navigate button's testid is
  // `mobile-route-job-nav-<id>`, which prefix-matches the card's
  // `mobile-route-job-<id>`. Selecting on the prefix alone grabs buttons too,
  // and a button has no href.
  const cards = page.locator('a[data-testid^="mobile-route-job-"]');
  const n = await cards.count();
  for (let i = 0; i < n; i++) {
    const href = await cards.nth(i).getAttribute('href');
    expect(href, `route card ${i} is not a link`).toMatch(/\/mobile\/jobs\/[0-9a-f-]{36}/);
  }
  console.log(`WALK ${BASE}: Today's Route rendered with ${n} stop card(s)`);
  if (n > 1) {
    // The multi-stop gap: every walk before this one saw exactly ONE stop, which
    // is how a stop-number badge with no CSS rule at all got through.
    const nums = await page.locator('.stop-num').allInnerTexts();
    console.log(`WALK: stop badges rendered: ${JSON.stringify(nums)}`);
    expect(nums.length, 'stop-number badges missing on a multi-stop route').toBeGreaterThan(1);
    const styled = await page.locator('.stop-num').first().evaluate((el) => {
      const cs = getComputedStyle(el);
      return { weight: cs.fontWeight, size: cs.fontSize, minWidth: cs.minWidth };
    });
    console.log('WALK: stop badge computed style:', JSON.stringify(styled));
    // The bug was: no rule at all, so it inherited defaults. 700 comes from .stop-num.
    expect(styled.weight, 'stop badge is unstyled — the CSS rule is not matching').toBe('700');
  }
});

test('the job detail screen carries the actions this release moved there', async ({ page }) => {
  await auth(page);
  await showAllJobs(page);
  const card = page.locator('[data-testid^="mobile-job-card-"]').first();
  await expect(card).toBeVisible({ timeout: 30000 });
  await card.click();
  await expect(page.locator('h1')).toContainText('Job details', { timeout: 20000 });

  // Chat is unguarded by status, so it must be present on any job the tech owns.
  // Observed only — never clicked: this is a real customer's job.
  const readOnly = await page.locator('[data-testid="mjd-readonly-banner"]').count();
  if (readOnly) {
    console.log('PROD: job opened view-only (company-scope browsing) — actions correctly hidden');
    await expect(page.locator('[data-testid="mjd-chat"]')).toHaveCount(0);
  } else {
    await expect(page.locator('[data-testid="mjd-secondary-actions"]')).toBeVisible();
    await expect(page.locator('[data-testid="mjd-chat"]')).toBeVisible();
  }
  await expect(page.locator('[data-testid="mjd-equipment-toggle"]')).toBeVisible();
});

test('light and dark on prod', async ({ page }) => {
  for (const theme of ['light', 'dark']) {
    await auth(page);
    await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);
    await showAllJobs(page);
    await expect(page.locator('[data-testid^="mobile-job-card-"]').first()).toBeVisible({ timeout: 30000 });
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    await page.screenshot({ path: `/home/doug/github_gdx_dispatch/scratch_e2e/prod-174-jobs-${theme}.png`, fullPage: false });
    await page.goto(`${BASE}/mobile`);
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `/home/doug/github_gdx_dispatch/scratch_e2e/prod-174-today-${theme}.png`, fullPage: false });
  }
});
