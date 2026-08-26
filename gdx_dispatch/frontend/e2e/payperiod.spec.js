/**
 * Pay periods, walked in a real browser against a throwaway container.
 *
 * jsdom applies no media queries and renders no layout, so the vitest suite
 * proves the bindings and nothing about whether a person can read this. This
 * spec drives the rendered SPA and captures LIGHT and DARK for both screens —
 * Doug uses dark mode, and a light-only pass is a half-check.
 *
 * It does NOT complete a successful send. That would put real mail through
 * the tenant's Outlook connection to an address nobody asked for. The refusal
 * path (which mails nothing) is walked here; delivery is proven on prod with
 * the real recipient.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'e2e-shots';

async function authed(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: {
      'content-type': 'application/json',
      'x-tenant-id': TENANT,
      'x-e2e-test': 'true',
    },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), 'login must succeed').toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
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

test.describe('Pay periods', () => {
  test('Timesheets opens on the pay period and names it', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');

    await expect(page.locator('[data-testid="timesheets-this-pay-period"]')).toBeVisible({ timeout: 20000 });
    await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible();

    // Opens on the CURRENT period, banner naming it and the pay date.
    const note = page.locator('[data-testid="timesheets-period-note"]');
    await expect(note).toBeVisible();
    await expect(note).toContainText('This pay period');

    await page.screenshot({ path: `${SHOTS}/timesheets-current-light.png`, fullPage: false });
    await api.dispose();
  });

  test('Last pay period loads the fortnight being paid', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible({ timeout: 20000 });

    await page.locator('[data-testid="timesheets-last-pay-period"]').click();
    await page.waitForTimeout(1200);

    const note = page.locator('[data-testid="timesheets-period-note"]');
    await expect(note).toContainText('Last pay period');
    await expect(note).toContainText('2026-08-10');
    await expect(note).toContainText('2026-08-23');
    await expect(note).toContainText('2026-08-28');   // the pay date

    // Real hours rendered, not an empty state.
    await expect(page.locator('[data-testid="timesheets-summary"]')).toBeVisible();
    await expect(page.locator('[data-testid="timesheets-empty"]')).toHaveCount(0);

    await page.screenshot({ path: `${SHOTS}/timesheets-last-period-light.png` });
    await setTheme(page, 'dark');
    await page.screenshot({ path: `${SHOTS}/timesheets-last-period-dark.png` });
    await api.dispose();
  });

  test('Export CSV downloads a real file with a real name', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible({ timeout: 20000 });
    await page.locator('[data-testid="timesheets-last-pay-period"]').click();
    await page.waitForTimeout(1200);

    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 30000 }),
      page.locator('[data-testid="timesheets-export-csv"]').click(),
    ]);
    // A blob URL carries no filename; downloadAuthedFile sets one.
    expect(download.suggestedFilename()).toBe('timesheet_2026-08-10_2026-08-23.csv');
    await download.saveAs(`${SHOTS}/walk-export.csv`);
    await api.dispose();
  });

  test('Export PDF downloads a real PDF', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible({ timeout: 20000 });
    await page.locator('[data-testid="timesheets-last-pay-period"]').click();
    await page.waitForTimeout(1200);

    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 45000 }),
      page.locator('[data-testid="timesheets-export-pdf"]').click(),
    ]);
    expect(download.suggestedFilename()).toBe('timesheet_2026-08-10_2026-08-23.pdf');
    await download.saveAs(`${SHOTS}/walk-export.pdf`);
    await api.dispose();
  });

  test('Send asks first, and states the range it is about to mail', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible({ timeout: 20000 });
    await page.locator('[data-testid="timesheets-last-pay-period"]').click();
    await page.waitForTimeout(1200);

    await page.locator('[data-testid="timesheets-send"]').click();
    const confirm = page.locator('[data-testid="send-confirm"]');
    await expect(confirm).toBeVisible();
    await expect(confirm).toContainText('2026-08-10 – 2026-08-23');

    await page.screenshot({ path: `${SHOTS}/send-confirm-light.png` });
    await setTheme(page, 'dark');
    await page.screenshot({ path: `${SHOTS}/send-confirm-dark.png` });
    await api.dispose();
  });

  test('every toolbar control stays on screen', async ({ page, baseURL }) => {
    // jsdom applies no media queries and lays nothing out, so this class of
    // bug is invisible to vitest. The first walk of this page clipped
    // "Send to payroll" and Add Entry off the right edge at 1280px.
    const api = await authed(page, baseURL);
    for (const width of [1280, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/timesheets');
      await expect(page.locator('[data-testid="timesheets-send"]')).toBeVisible({ timeout: 20000 });

      const overflowing = await page.evaluate(() => {
        const ids = [
          'timesheets-export-csv', 'timesheets-export-pdf',
          'timesheets-send', 'timesheets-add-entry', 'timesheets-refresh',
        ];
        const bad = [];
        for (const id of ids) {
          const el = document.querySelector(`[data-testid="${id}"]`);
          if (!el) { bad.push(`${id}:missing`); continue; }
          const r = el.getBoundingClientRect();
          if (r.right > window.innerWidth + 1 || r.left < -1) {
            bad.push(`${id}:${Math.round(r.left)}-${Math.round(r.right)}/${window.innerWidth}`);
          }
        }
        return bad;
      });
      expect(overflowing, `controls clipped at ${width}px`).toEqual([]);
      await page.screenshot({ path: `${SHOTS}/toolbar-${width}.png` });
    }
    await api.dispose();
  });

  test('a disabled Send does not read as a live button', async ({ page, baseURL }) => {
    // This repo has a recorded trap: the theme renders a disabled PRIMARY
    // button almost identically to an enabled one, so PayrollView's "Run
    // payroll" was REMOVED rather than disabled — it invited a click that
    // silently did nothing. "Send to payroll" is a primary button that is
    // disabled on an empty range, so the same trap applies. Measure it.
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="timesheets-send"]')).toBeVisible({ timeout: 20000 });

    const m = await page.evaluate(() => {
      const g = (id) => {
        const e = document.querySelector(`[data-testid="${id}"]`);
        const c = getComputedStyle(e);
        return { disabled: !!e.disabled, opacity: parseFloat(c.opacity), cursor: c.cursor };
      };
      return { send: g('timesheets-send'), add: g('timesheets-add-entry') };
    });

    expect(m.send.disabled, 'an empty range must not offer to send').toBe(true);
    expect(m.add.disabled).toBe(false);
    // Visibly different, not merely inert.
    expect(m.send.opacity, 'a disabled Send must look disabled')
      .toBeLessThan(m.add.opacity);
    await api.dispose();
  });

  test('an unresolved shift holds the send and names it on screen', async ({ page, baseURL }) => {
    // The whole reason the gate exists. A period with an open shift is a
    // draft, and mailing it puts a number in front of somebody who will act
    // on it. Nothing is emailed on this path.
    const api = await authed(page, baseURL);
    await page.goto('/timesheets');
    await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible({ timeout: 20000 });
    await page.locator('[data-testid="timesheets-last-pay-period"]').click();
    await page.waitForTimeout(1200);

    await page.locator('[data-testid="timesheets-send"]').click();
    await page.locator('[data-testid="send-confirm-button"]').click();

    const blocked = page.locator('[data-testid="send-blocked"]');
    await expect(blocked).toBeVisible({ timeout: 20000 });
    await expect(blocked).toContainText('a look');
    await expect(blocked).toContainText('2026-08-19');   // the offending day
    // No way out except fixing it.
    await expect(page.locator('[data-testid="send-confirm-button"]')).toHaveCount(0);

    await page.screenshot({ path: `${SHOTS}/send-held-light.png` });
    await setTheme(page, 'dark');
    await page.screenshot({ path: `${SHOTS}/send-held-dark.png` });
    await api.dispose();
  });

  test('Settings shows the payroll calendar it will actually use', async ({ page, baseURL }) => {
    const api = await authed(page, baseURL);
    await page.goto('/settings');
    await page.waitForTimeout(2500);

    // The card sits in the "Feature Settings" tab, beside Shop hours — the
    // closest existing sibling. Tab panels are lazy, so it is not merely
    // scrolled off screen until that tab is selected.
    await page.getByRole('tab', { name: 'Feature Settings' }).click();
    await page.waitForTimeout(1500);

    const card = page.locator('[data-testid="pay-period-card"]');
    await card.scrollIntoViewIfNeeded();
    await expect(card).toBeVisible({ timeout: 20000 });

    const preview = page.locator('[data-testid="pay-period-preview"]');
    await expect(preview).toBeVisible();
    await expect(preview).toContainText('2026-08-10 – 2026-08-23');
    await expect(preview).toContainText('2026-08-28');

    await card.screenshot({ path: `${SHOTS}/settings-payperiod-light.png` });
    await setTheme(page, 'dark');
    await card.screenshot({ path: `${SHOTS}/settings-payperiod-dark.png` });
    await api.dispose();
  });
});
