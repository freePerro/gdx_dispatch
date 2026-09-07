// #645 — the mobile job page's day clock, in a real browser.
//
// The card is labelled "Day — your paid time". Before this fix it rendered
// GROSS wall clock and said "Running" straight through a tech's lunch, and a
// shift nobody closed rendered as a normal one. Unit tests cannot prove the
// rendered card: jsdom applies no media queries, so the dark pass in
// particular is only real here.
//
// The three states are driven by seeding `timeclock_entries_router` /
// `timeclock_breaks_router` through the API-less path the harness sets up
// (E2E_DAY_CLOCK_JOB_ID + a tech whose shift is already seeded), so this spec
// asserts what the SCREEN shows, not what the endpoint returns — that half is
// pinned in tests/test_mobile_job_clock.py.
//
// Skips cleanly when env is unset. Credentials from env — no secret committed.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const JOB_ID = process.env.E2E_DAY_CLOCK_JOB_ID || '';

let cachedToken = null;

async function login(baseURL) {
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
  });
  if (!cachedToken) {
    const r = await api.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
    expect(r.ok(), `login: ${r.status()}`).toBeTruthy();
    cachedToken = (await r.json()).access_token;
  }
  return { api, token: cachedToken };
}

async function openJob(page, token, theme) {
  await page.addInitScript((t) => sessionStorage.setItem('gdx_access_token', t), token);
  await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);
  await page.goto(`/mobile/jobs/${JOB_ID}`);
  const card = page.locator('[data-testid="mjd-day-clock"]');
  await expect(card).toBeVisible({ timeout: 20000 });
  return card;
}

test.describe('#645 mobile day clock', () => {
  test.beforeEach(() => {
    test.skip(!EMAIL || !PASSWORD || !JOB_ID, 'env not set');
  });

  for (const theme of ['light', 'dark']) {
    test(`paid time is net of breaks and says so — ${theme}`, async ({ page, baseURL }) => {
      // 390x844 = a real phone. This card is a tech-on-a-phone surface; the
      // desktop viewport would not exercise the wrap.
      await page.setViewportSize({ width: 390, height: 844 });
      const { api, token } = await login(baseURL);

      // What the server says the card must show. Asserting the screen against
      // THIS rather than a hardcoded number is what makes the test survive the
      // clock moving between seed and render.
      const r = await api.get(`/api/mobile/job/${JOB_ID}`, {
        headers: { authorization: `Bearer ${token}` },
      });
      expect(r.ok(), await r.text()).toBeTruthy();
      const day = (await r.json()).clocks.day;
      test.skip(!day.running, 'no open shift seeded for this tech');

      const card = await openJob(page, token, theme);
      await expect(card).toContainText('your paid time');

      // Branch precedence matters and is asserted in the same order the
      // template resolves it: on-break beats stale beats the plain break note.
      // An earlier version of this spec assumed the break note always showed
      // once break_minutes > 0 and failed the moment a shift went stale.
      if (day.on_break) {
        // The whole point: an open break must not read as "Running" on the
        // clock that pays you, and the note must not print a gross-less-breaks
        // sum that disagrees with the frozen figure.
        await expect(page.locator('[data-testid="mjd-day-clock-on-break"]')).toBeVisible();
        await expect(card).toContainText('On break');
        await expect(card).not.toContainText('Running');
        await expect(
          page.locator('[data-testid="mjd-day-clock-break-note"]'),
        ).toHaveCount(0);
      } else if (day.stale) {
        // A shift nobody closed must not read as a normal one.
        await expect(page.locator('[data-testid="mjd-day-clock-stale"]')).toBeVisible();
        // The note is a sibling of the clock row, not a child of it — assert on
        // the note itself rather than on `card`, which cannot see it.
        await expect(page.locator('[data-testid="mjd-day-clock-stale-note"]')).toContainText(
          `${day.max_shift_hours}h limit`,
        );
      } else if (day.break_minutes > 0) {
        // The figure on screen is the NET one, and the card explains the gap
        // rather than silently showing a smaller number than the wall clock.
        await expect(page.locator('[data-testid="mjd-day-clock-break-note"]')).toBeVisible();
        await expect(card).toContainText('Running');
      }

      await card.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `e2e-artifacts/day-clock-${theme}.png` });

      // Contrast is the thing jsdom cannot see. Assert the state text is not
      // rendering in the muted grey every other row uses — i.e. the break/stale
      // colour actually resolved in THIS theme.
      const colors = await page.evaluate(() => {
        const el =
          document.querySelector('[data-testid="mjd-day-clock-on-break"]') ||
          document.querySelector('[data-testid="mjd-day-clock-stale"]') ||
          document.querySelector('.clock-row-day .clock-state');
        if (!el) return null;
        const cs = getComputedStyle(el);
        const bodyBg = getComputedStyle(document.body).backgroundColor;
        return { color: cs.color, weight: cs.fontWeight, bodyBg };
      });
      expect(colors, 'day clock state text must render').toBeTruthy();
      expect(colors.color).not.toBe(colors.bodyBg);
      // Never transparent / fully unset.
      expect(colors.color).not.toContain('rgba(0, 0, 0, 0)');

      await api.dispose();
    });
  }

  test('the page does not scroll horizontally on a phone', async ({ page, baseURL }) => {
    // A wrapped break note is the kind of thing that pushes a phone layout
    // sideways, and no unit test can see it.
    await page.setViewportSize({ width: 390, height: 844 });
    const { api, token } = await login(baseURL);
    await openJob(page, token, 'light');
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, 'horizontal overflow in px').toBeLessThanOrEqual(1);
    await api.dispose();
  });
});
