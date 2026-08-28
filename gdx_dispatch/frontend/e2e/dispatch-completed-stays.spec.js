/**
 * Dispatch board — a finished job stays on the board (Doug, 2026-08-28).
 *
 * Walks the actual report: "after a job is finished it disappears from the
 * dispatch board, there is no way to go back and look."
 *
 * Sequence, in a real browser against the throwaway serving the new dist:
 *   1. create a job scheduled TODAY, assigned to a real technician
 *   2. board renders it in that tech's column          → screenshot
 *   3. POST /api/jobs/{id}/complete  (the real endpoint)
 *   4. reload — the card is STILL THERE                → screenshot (light+dark)
 *   5. mobile viewport, still there                    → screenshot
 *   6. clean up: uncomplete + delete the seeded job
 *
 * Before the fix the card vanished at step 4 and no control on the page could
 * bring it back: the "Show Completed Jobs" button lived inside
 * `v-if="skillOptions.length"`, and every technician has skills = NULL.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || '.';

test('a completed job stays visible on the dispatch board', async ({ page, baseURL }) => {
  test.setTimeout(120000);
  await page.setViewportSize({ width: 1600, height: 1200 });

  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token } = await login.json();
  const auth = {
    authorization: `Bearer ${access_token}`,
    'x-tenant-id': TENANT,
    'content-type': 'application/json',
    'x-e2e-test': 'true',
  };

  // --- Seed: a job today, on a real tech ------------------------------------
  const techs = await (await api.get('/api/technicians', { headers: auth })).json();
  const tech = (Array.isArray(techs) ? techs : techs.items || []).find((t) => t.active !== false);
  expect(tech, 'need an active technician').toBeTruthy();

  const customers = await (await api.get('/api/customers?per_page=1', { headers: auth })).json();
  const customer = (Array.isArray(customers) ? customers : customers.items || [])[0];
  expect(customer, 'need a customer').toBeTruthy();

  const now = new Date();
  const at10 = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 10, 0, 0);
  const stamp = Date.now();
  const TITLE = `E2E completed-stays ${stamp}`;

  const created = await api.post('/api/jobs', {
    headers: auth,
    data: {
      title: TITLE,
      customer_id: customer.id,
      job_type: 'Service Call',
      scheduled_at: at10.toISOString(),
      technician_id: tech.id,
      assigned_to: tech.id,
      scheduled_duration_hours: 2,
    },
  });
  expect(created.ok()).toBeTruthy();
  const job = await created.json();

  // --- Prime auth the way the SPA expects (never touch the login form) ------
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });

  const card = page.locator(`[data-testid="timeline-job-${job.id}"]`);

  // --- 1. Before completion: on the board ----------------------------------
  await page.goto('/dispatch');
  await expect(card).toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: `${SHOTS}/dispatch-before-complete.png`, fullPage: false });

  // --- 2. Complete it through the real endpoint ----------------------------
  const done = await api.post(`/api/jobs/${job.id}/complete`, { headers: auth, data: {} });
  expect(done.ok(), `complete failed: ${done.status()} ${await done.text()}`).toBeTruthy();

  // Confirm the server really flipped it — otherwise this proves nothing.
  const after = await (await api.get(`/api/jobs/${job.id}`, { headers: auth })).json();
  expect(String(after.lifecycle_stage || after.status).toLowerCase()).toMatch(/complete/);

  // --- 3. THE REGRESSION: reload, card must still be there -----------------
  await page.reload();
  await expect(card).toBeVisible({ timeout: 30000 });
  await expect(card).toContainText(/E2E completed-stays/);
  await page.screenshot({ path: `${SHOTS}/dispatch-after-complete-light.png`, fullPage: false });

  // --- 4. Dark mode --------------------------------------------------------
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.reload();
  await expect(card).toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: `${SHOTS}/dispatch-after-complete-dark.png`, fullPage: false });

  // --- 5. Mobile viewport --------------------------------------------------
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(card).toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: `${SHOTS}/dispatch-after-complete-mobile.png`, fullPage: true });

  // --- Cleanup: put the dev DB back ----------------------------------------
  await api.post(`/api/jobs/${job.id}/uncomplete`, { headers: auth, data: {} });
  await api.delete(`/api/jobs/${job.id}`, { headers: auth });
});
