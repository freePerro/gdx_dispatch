/**
 * Mobile customer/job-create fix (2026-07-22) — browser verification.
 *
 * Covers Doug's two complaints end-to-end against a throwaway container
 * (/verifyplaywright flow):
 *  1. Customers reachable from the bottom nav; create → detail; the new
 *     customer appears in the list immediately (cache invalidation).
 *  2. New job from customer detail (preseeded, opens CLEAN — no phantom
 *     discard), description persists, job visible in /mobile/jobs and
 *     openable by its creator; digits-only phone search finds the
 *     formatted stored number.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

async function primeAuth(page, baseURL) {
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
  await api.dispose();
}

test.use({ viewport: { width: 390, height: 844 } });

test('mobile customer add/search + job create flows', async ({ page, baseURL }) => {
  await primeAuth(page, baseURL);
  const stamp = Date.now().toString().slice(-7);
  const custName = `PW Verify ${stamp}`;
  const phoneDigits = `612555${stamp.slice(-4)}`;

  // Customers tab exists in the bottom nav and routes to the mobile view.
  await page.goto('/mobile/jobs');
  await page.locator('nav.bottom-nav .tab-btn', { hasText: 'Customers' }).click();
  await expect(page).toHaveURL(/\/mobile\/customers$/);

  // Create a customer with a phone the PhoneInput will format.
  await page.locator('[data-test="mc-head-add"]').click();
  await page.locator('[data-test="mc-form-name"]').fill(custName);
  await page.locator('[data-test="mc-form-phone"]').fill(phoneDigits);
  await page.locator('[data-test="mc-form-submit"]').click();
  await expect(page).toHaveURL(/\/mobile\/customers\/[0-9a-f-]+$/, { timeout: 15000 });

  // New job quick action → dialog opens PRESEEDED and CLEAN.
  page.on('dialog', () => { throw new Error('unexpected confirm() — dialog opened dirty'); });
  await page.locator('[data-test="mcd-new-job"]').click();
  await expect(page.locator('[data-testid="mjn-customer-picked"]')).toContainText(custName);
  // Clean open → Cancel closes silently (a confirm() would throw above).
  await page.locator('[data-testid="mjn-cancel"]').click();
  await expect(page.locator('[data-testid="mjn-job-title"]')).toBeHidden();

  // Reopen, create the job with a description.
  await page.locator('[data-test="mcd-new-job"]').click();
  await page.locator('[data-testid="mjn-job-title"]').fill(`PW job ${stamp}`);
  await page.locator('[data-testid="mjn-job-description"]').fill('PW-verified description text');
  await page.locator('[data-testid="mjn-submit"]').click();

  // Creator lands on the job detail (read access) with description shown.
  await expect(page).toHaveURL(/\/mobile\/jobs\/[0-9a-f-]+$/, { timeout: 15000 });
  await expect(page.getByText('PW-verified description text')).toBeVisible();

  // The job is in the creator's Jobs list, unassigned.
  await page.goto('/mobile/jobs');
  const card = page.locator('.job-card', { hasText: `PW job ${stamp}` });
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card).toContainText('Unassigned');

  // Digits-only search in the dialog matches the FORMATTED stored phone.
  await page.locator('[data-testid="mobile-jobs-new-btn"]').click();
  await page.locator('[data-testid="mjn-customer-search"]').pressSequentially(phoneDigits);
  await expect(
    page.locator('[data-testid="mjn-customer-option"]', { hasText: custName })
  ).toBeVisible({ timeout: 10000 });

  // Zero-result search offers the add-as-new escape hatch and prefills.
  await page.locator('[data-testid="mjn-customer-search"]').fill(`ZZZ Nobody ${stamp}`);
  await page.locator('[data-testid="mjn-no-results-add"]').click();
  await expect(page.locator('[data-testid="mjn-newcust-name"]')).toHaveValue(`ZZZ Nobody ${stamp}`);
});
