/**
 * Prod walk — pay periods on gdx.teamgaragedoor.com.
 *
 * Read-only. Opens Timesheets and Settings as the owner and looks at what a
 * person would actually see. Deliberately does NOT press Send: that mails a
 * real third party, and whether to do that is the owner's call, not a test's.
 *
 * No login page is touched — the session is primed directly — so no password
 * is ever typed or captured. (A prior session leaked a plaintext password by
 * snapshotting the login page with autofill active.)
 */
import { test, expect } from '@playwright/test';

const SESSION = process.env.PW_PROD_SESSION;
const SHOTS = process.env.E2E_SHOT_DIR || 'prod-shots';

async function authed(page) {
  await page.addInitScript((t) => {
    sessionStorage.setItem('gdx_access_token', t);
    sessionStorage.setItem('gdx_tenant_slug', 'gdx');
  }, SESSION);
}

async function setTheme(page, theme) {
  await page.evaluate((t) => {
    document.documentElement.classList.toggle('dark', t === 'dark');
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('theme', t); } catch { /* ignore */ }
  }, theme);
  await page.waitForTimeout(400);
}

test('Timesheets shows the real fortnight being paid', async ({ page }) => {
  await authed(page);
  await page.goto('https://gdx.teamgaragedoor.com/timesheets');

  await expect(page.locator('[data-testid="timesheets-last-pay-period"]')).toBeVisible({ timeout: 30000 });
  await page.locator('[data-testid="timesheets-last-pay-period"]').click();
  await page.waitForTimeout(2500);

  const note = page.locator('[data-testid="timesheets-period-note"]');
  await expect(note).toContainText('2026-08-10');
  await expect(note).toContainText('2026-08-23');
  await expect(note).toContainText('2026-08-28');

  await expect(page.locator('[data-testid="timesheets-summary"]')).toContainText('74.22');

  await page.screenshot({ path: `${SHOTS}/prod-timesheets-light.png` });
  await setTheme(page, 'dark');
  await page.screenshot({ path: `${SHOTS}/prod-timesheets-dark.png` });
});

test('Settings shows the payroll calendar it will use', async ({ page }) => {
  await authed(page);
  await page.goto('https://gdx.teamgaragedoor.com/settings');
  await page.waitForTimeout(3000);
  await page.getByRole('tab', { name: 'Feature Settings' }).click();
  await page.waitForTimeout(2000);

  const card = page.locator('[data-testid="pay-period-card"]');
  await card.scrollIntoViewIfNeeded();
  await expect(card).toBeVisible({ timeout: 30000 });
  await expect(page.locator('[data-testid="pay-period-preview"]')).toContainText('2026-08-10 – 2026-08-23');

  await card.screenshot({ path: `${SHOTS}/prod-settings-light.png` });
  await setTheme(page, 'dark');
  await card.screenshot({ path: `${SHOTS}/prod-settings-dark.png` });
});
