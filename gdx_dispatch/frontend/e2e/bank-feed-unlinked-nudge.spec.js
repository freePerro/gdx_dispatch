/**
 * Bank Feeds — the unlinked-account nudge.
 *
 * The match-status column reports `not linked` honestly, but nothing brought
 * anyone to the tab that fixes it: a newly synced account could sit unlinked
 * forever. This proves the prompt exists and counts only accounts worth
 * acting on.
 *
 * It deliberately does NOT propose which statement account to pick. An earlier
 * draft offered a one-click "Link to X" derived from a last-4 in the account
 * name; an adversarial audit showed that is the same display-name inference
 * the status column exists to avoid — `bank_accounts` is unique on
 * (institution, last4), so two banks can both end 2204, and the feed and
 * statement sides share no institution vocabulary to disambiguate with
 * ("SimpleFIN Bridge" vs "Primary Bank"). Prompting is safe; proposing a money
 * pairing off a renameable string is not.
 *
 * Requires the seeded throwaway (see /verifyplaywright).
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

test('unlinked accounts are surfaced without proposing which one to pick', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1200 });
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });

  await page.goto('/bank-feeds');

  // The badge is the part that works without opening the tab.
  const badge = page.getByTestId('bank-feeds-unlinked-badge');
  await expect(badge).toBeVisible({ timeout: 15000 });
  // Both seeded feed accounts sync and carry transactions, so both count.
  await expect(badge).toHaveText('2');

  await page.getByTestId('bank-feeds-tab-accounts').click();
  const banner = page.getByTestId('bank-feeds-unlinked-banner');
  await expect(banner).toBeVisible({ timeout: 15000 });
  await expect(banner).toContainText("can't be checked against the bank's own statement");
  await expect(banner).toContainText('Garage Door inc (2204)');
  await expect(banner).toContainText('Random Squirell (8988)');
  // It names the cost, and points at the control — it does not pick for you.
  await expect(banner).toContainText('transactions unchecked');
  await expect(banner).toContainText('Statement account');
  expect(await page.locator('[data-testid^="bank-feeds-accept-hint-"]').count()).toBe(0);

  await page.screenshot({ path: 'test-results/nudge-light.png' });

  // Linking through the picker is what shrinks the prompt.
  const feedId = await page.evaluate(() => {
    const el = [...document.querySelectorAll('[data-testid^="bank-feeds-statement-link-"]')]
      .find((e) => e.textContent.includes('Not linked'));
    return el ? el.dataset.testid.replace('bank-feeds-statement-link-', '') : null;
  });
  expect(feedId).toBeTruthy();
  const res = await api.patch(`/api/bank-feeds/accounts/${feedId}/statement-link`, {
    headers: {
      'content-type': 'application/json',
      'x-tenant-id': TENANT,
      authorization: `Bearer ${access_token}`,
    },
    data: { bank_account_id: await page.evaluate(async () => {
      const r = await fetch('/api/bank-feeds/statements/accounts', {
        headers: {
          authorization: 'Bearer ' + sessionStorage.getItem('gdx_access_token'),
          'x-tenant-id': sessionStorage.getItem('gdx_tenant_slug'),
        },
      });
      const d = await r.json();
      return (d.items.find((a) => a.last4 === '2204') || {}).id;
    }) },
  });
  expect(res.ok()).toBeTruthy();
  await page.reload();
  await page.getByTestId('bank-feeds-tab-accounts').click();
  await expect(badge).toHaveText('1', { timeout: 15000 });

  // And the statuses it drives are recomputed, not left stale on screen.
  await page.getByTestId('bank-feeds-tab-transactions').click();
  await expect(page.locator('[data-testid^="bank-feeds-txn-status-"]').first())
    .toBeVisible({ timeout: 15000 });
  // Poll: the link triggers a reload, so the rows already painted are the
  // pre-link ones for a moment. Reading once races that repaint.
  await expect.poll(async () => page.evaluate(() => {
    const t = document.querySelector('[data-testid="bank-feeds-transactions-table"]');
    if (!t) return 0;
    return [...t.querySelectorAll('[data-testid^="bank-feeds-txn-status-"]')]
      .filter((e) => e.innerText.trim() === 'statement-verified').length;
  }), { timeout: 15000 }).toBeGreaterThan(0);

  // Dark mode: the banner is a colour-carrying surface.
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto('/bank-feeds');
  await page.getByTestId('bank-feeds-tab-accounts').click();
  await expect(page.getByTestId('bank-feeds-unlinked-banner')).toBeVisible({ timeout: 15000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await page.screenshot({ path: 'test-results/nudge-dark.png' });
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));

  await api.dispose();
});
