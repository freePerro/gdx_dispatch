/**
 * Bank Feeds — statement-account link + feed match status column.
 *
 * Books-convergence Track 2 item 4. Proves in a rendered browser that:
 *  - the Accounts tab exposes a statement-account picker and it persists;
 *  - the Transactions tab renders a Statement status per row;
 *  - two identical charges against ONE statement line both read `ambiguous`
 *    rather than both going green. That false-green was the real bug an
 *    adversarial audit found in the first draft, so it gets a browser guard,
 *    not just a unit test.
 *
 * Requires the seeded throwaway (see /verifyplaywright): 9 transactions built
 * to land on one status each.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

async function signIn(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: {
      'content-type': 'application/json',
      'x-e2e-test': 'true',
    },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: access_token, tid: TENANT });
  return api;
}

/** payee -> rendered status tag text, read off the Transactions table. */
async function statusByPayee(page) {
  return page.evaluate(() => {
    const out = {};
    const table = document.querySelector('[data-testid="bank-feeds-transactions-table"]');
    if (!table) return out;
    for (const tr of table.querySelectorAll('tbody tr')) {
      const cells = [...tr.querySelectorAll('td')];
      if (cells.length < 2) continue;
      const payee = cells[1]?.innerText.trim();
      const tag = tr.querySelector('[data-testid^="bank-feeds-txn-status-"]');
      if (payee) out[payee] = tag ? tag.innerText.trim() : '—';
    }
    return out;
  });
}

test('statement link drives the match status column', async ({ page, baseURL }) => {
  // Tall viewport: the app scrolls an inner container, so Playwright's
  // fullPage stops at the viewport and the lower rows never appear in the
  // evidence screenshot.
  await page.setViewportSize({ width: 1440, height: 1400 });
  const api = await signIn(page, baseURL);

  await page.goto('/bank-feeds');
  await page.getByTestId('bank-feeds-tab-accounts').click();
  await expect(page.getByTestId('bank-feeds-accounts-table')).toBeVisible({ timeout: 15000 });

  // The picker exists on every feed account row. Wait for a row rather than
  // the table: the table renders its EmptyState before the accounts land, so
  // counting on table-visible alone races the fetch.
  const pickers = page.locator('[data-testid^="bank-feeds-statement-link-"]');
  await expect(pickers.first()).toBeVisible({ timeout: 15000 });
  expect(await pickers.count()).toBeGreaterThan(0);
  // The link set earlier is shown back to the operator, not just stored.
  await expect(page.getByTestId('bank-feeds-accounts-table'))
    .toContainText('Business Checking ····2204');

  await page.getByTestId('bank-feeds-tab-transactions').click();
  await expect(page.locator('[data-testid^="bank-feeds-txn-status-"]').first())
    .toBeVisible({ timeout: 15000 });

  const seen = await statusByPayee(page);
  // Every seeded row lands on the status it was built to land on.
  expect(seen['VERIFIED-rent']).toBe('statement-verified');
  expect(seen['MATCHED-fuel']).toBe('matched · bank_fee');
  expect(seen['UNMATCHED-mystery']).toBe('unmatched');
  expect(seen['FEEDONLY-august']).toBe('feed-only');
  expect(seen['NOAMOUNT-row']).toBe('no amount');
  expect(seen['UNLINKED-other']).toBe('not linked');
  expect(seen['PENDING-row']).toBe('pending');

  // The load-bearing one: one statement line, two indistinguishable charges.
  expect(seen['AMBIG-coffee-a']).toBe('ambiguous · 2 for 1');
  expect(seen['AMBIG-coffee-b']).toBe('ambiguous · 2 for 1');
  // Neither is allowed to claim the bank confirmed it.
  expect(seen['AMBIG-coffee-a']).not.toContain('verified');
  expect(seen['AMBIG-coffee-b']).not.toContain('verified');

  await page.screenshot({ path: 'test-results/bank-feed-status-light.png', fullPage: true });

  // Dark mode: Doug runs the app dark, and status tags are colour-carrying.
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto('/bank-feeds');
  await page.getByTestId('bank-feeds-tab-transactions').click();
  await expect(page.locator('[data-testid^="bank-feeds-txn-status-"]').first())
    .toBeVisible({ timeout: 15000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  const dark = await statusByPayee(page);
  expect(dark['AMBIG-coffee-a']).toBe('ambiguous · 2 for 1');
  expect(dark['VERIFIED-rent']).toBe('statement-verified');
  await page.screenshot({ path: 'test-results/bank-feed-status-dark.png', fullPage: true });

  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));
  await api.dispose();
});
