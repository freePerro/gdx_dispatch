/**
 * PR B — one job card, and the route card is finally tappable.
 *
 * The complaint this closes: "in the mobile app the todays route you cannot
 * click on the job to get to the job. it is like it is a different card than
 * the job card." It literally was — three separately-written markups, and the
 * primary one was a plain <li> with no link.
 *
 * Walked at a phone viewport against a throwaway serving this branch's dist.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const SECRET = process.env.E2E_PASSWORD;
const JOB = process.env.E2E_JOB_ID;

test.use({ viewport: { width: 375, height: 812 } });

test.beforeEach(() => {
  test.skip(!JOB, 'E2E_JOB_ID not set — see the seed recipe in the one-job-card plan');
});

async function auth(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: SECRET },
  });
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: body.access_token, tid: TENANT });
  await api.dispose();
}

test('the Jobs list card opens the job', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.goto('/mobile/jobs');
  const card = page.locator('[data-testid^="mobile-job-card-"]').first();
  await expect(card).toBeVisible({ timeout: 20000 });
  await card.click();
  await expect(page).toHaveURL(/\/mobile\/jobs\/[0-9a-f-]{36}/, { timeout: 15000 });
  await expect(page.locator('h1')).toContainText('Job details');
});

test('the navigate button is a sibling of the link, so one tap does one thing', async ({ page, baseURL }) => {
  // The blind spot the pre-code audit caught: Today's route card carried
  // @click="openMaps" on the address text. Wrapping the card in a link would
  // either delete tap-to-navigate or fire both on one tap. The nav control is
  // now a real <button> OUTSIDE the anchor.
  await auth(page, baseURL);
  // The ROUTE, not the Jobs list: the seeded walk job is guaranteed to have an
  // address there, so this guard actually runs. Pointing it at the Jobs list
  // let it skip whenever the first listed job happened to have no address —
  // and a guard that skips is not a guard.
  await page.goto('/mobile');
  const nav = page.locator('[data-testid^="mobile-route-job-nav-"]').first();
  await expect(nav, 'the seeded route job must have a navigation_link').toBeVisible({ timeout: 20000 });

  const box = await nav.boundingBox();
  expect(box.height, `nav target ${box.height}px`).toBeGreaterThanOrEqual(44);
  expect(box.width, `nav target ${box.width}px`).toBeGreaterThanOrEqual(44);

  // It must NOT be inside the anchor, or the two gestures fight.
  const nested = await nav.evaluate((el) => !!el.closest('a'));
  expect(nested, 'the navigate button must not be inside the card link').toBe(false);

  // And tapping it must not navigate the SPA.
  const before = page.url();
  await nav.click();
  await page.waitForTimeout(800);
  expect(page.url()).toBe(before);
});

test("Today's Route card opens the job — the whole point of this change", async ({ page, baseURL }) => {
  // The original complaint, asserted as the user experiences it: tap the card
  // on the route screen, land on that job. Deliberately NOT an elementFromPoint
  // probe — an earlier version of this test scrolled a card under the fixed
  // topbar and failed on its own artifact. Playwright's click carries real
  // actionability checks (visible, stable, receives events), so if anything
  // genuinely covers the card, this fails for the right reason.
  await auth(page, baseURL);
  await page.goto('/mobile');
  const card = page.locator('[data-testid^="mobile-route-job-"]').first();
  await expect(card, "the route has no stops — seed a job for the logged-in tech")
    .toBeVisible({ timeout: 20000 });
  await card.click();
  await expect(page).toHaveURL(/\/mobile\/jobs\/[0-9a-f-]{36}/, { timeout: 15000 });
  await expect(page.locator('h1')).toContainText('Job details');
});

test('cards in the initial viewport are not covered by anything', async ({ page, baseURL }) => {
  // No scrolling: this asks whether the page as first painted hands the tech a
  // tappable card, which is the state that matters and the one a fixed header
  // or FAB can silently ruin.
  await auth(page, baseURL);
  for (const route of ['/mobile', '/mobile/jobs']) {
    await page.goto(route);
    await page.waitForTimeout(2500);
    const covered = await page.evaluate(() => {
      const sel = '[data-testid^="mobile-route-job-"], [data-testid^="mobile-area-job-"], [data-testid^="mobile-job-card-"]';
      const bad = [];
      document.querySelectorAll(sel).forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        if (r.top < 0 || r.top + 24 > window.innerHeight) return;   // off-screen: not this test's business
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + 24);
        if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) {
          bad.push(`${el.getAttribute('data-testid')} <- ${hit ? hit.tagName.toLowerCase() : 'null'}`);
        }
      });
      return bad;
    });
    expect(covered, `${route}: ${covered.join(' | ')}`).toEqual([]);
  }
});

test('dark mode: the shared card is readable on both surfaces', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto('/mobile/jobs');
  await expect(page.locator('[data-testid^="mobile-job-card-"]').first()).toBeVisible({ timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await page.screenshot({ path: 'test-results/prb-jobs-dark.png', fullPage: true });
  await page.goto('/mobile');
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'test-results/prb-today-dark.png', fullPage: true });
});

test('light mode screenshots for both surfaces', async ({ page, baseURL }) => {
  await auth(page, baseURL);
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'light'));
  await page.goto('/mobile');
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'test-results/prb-today-light.png', fullPage: true });
  await page.goto('/mobile/jobs');
  await expect(page.locator('[data-testid^="mobile-job-card-"]').first()).toBeVisible({ timeout: 20000 });
  await page.screenshot({ path: 'test-results/prb-jobs-light.png', fullPage: true });
});
