// Plan §1 — the closeout read side, rendered against REAL data.
//
// job_closeouts was write-only for 2.5 months: hours, work notes, the parts
// attestation and the signer name never reached a screen. This asserts the
// desktop closeout card renders a real closed-out job's attested figures, in
// both themes, and that the mobile detail payload now carries the summary.
//
// The job under test comes from E2E_CLOSEOUT_JOB_ID (a job in the target
// tenant with a live closeout) so the spec runs against real rows rather
// than seeding synthetic ones. Skips cleanly when unset.
//
// Credentials from env — no secret committed.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const JOB_ID = process.env.E2E_CLOSEOUT_JOB_ID || '';

let cachedToken = null;

async function login(baseURL) {
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: {
      'content-type': 'application/json',
      'x-e2e-test': 'true',
    },
  });
  if (!cachedToken) {
    const r = await api.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
    expect(r.ok(), `login: ${r.status()}`).toBeTruthy();
    cachedToken = (await r.json()).access_token;
  }
  return { api, token: cachedToken };
}

async function primeSession(page, token) {
  await page.addInitScript(
    (a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
    },
    { t: token, tid: TENANT },
  );
}

test.describe('closeout card — real data', () => {
  for (const theme of ['light', 'dark']) {
    test(`desktop card renders a real closeout in ${theme} mode`, async ({ page, baseURL }) => {
      test.skip(!EMAIL || !PASSWORD || !JOB_ID, 'env not set');
      const { api, token } = await login(baseURL);
      await primeSession(page, token);
      await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);

      // What the API says the card should show.
      const r = await api.get(`/api/jobs/${JOB_ID}/closeout`, {
        headers: { authorization: `Bearer ${token}` },
      });
      expect(r.ok(), await r.text()).toBeTruthy();
      const payload = await r.json();
      expect(payload.closeout, 'target job must have a live closeout').toBeTruthy();
      // A4: the raw blob is never serialized.
      expect(JSON.stringify(payload)).not.toContain('signature_data');

      await page.goto(`/jobs/${JOB_ID}`);
      const card = page.locator('[data-testid="job-closeout-card"]');
      await expect(card).toBeVisible({ timeout: 20000 });
      await expect(page.locator('[data-testid="closeout-hours"]')).toContainText(
        Number(payload.closeout.hours_worked).toFixed(2),
      );
      await expect(page.locator('[data-testid="closeout-signature"]')).toBeVisible();

      await card.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `e2e-artifacts/closeout-card-${theme}.png` });
      await api.dispose();
    });
  }

  test('mobile detail payload carries the closeout summary', async ({ baseURL }) => {
    test.skip(!EMAIL || !PASSWORD || !JOB_ID, 'env not set');
    const { api, token } = await login(baseURL);
    const r = await api.get(`/api/mobile/job/${JOB_ID}`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(r.ok(), await r.text()).toBeTruthy();
    // The mobile detail response nests the job under a `job` key.
    const body = await r.json();
    const job = body.job || body;
    expect(job.closeout, 'mobile payload must include the closeout summary').toBeTruthy();
    expect(typeof job.closeout.hours_worked).toBe('number');
    expect(JSON.stringify(job.closeout)).not.toContain('signature_data');
    await api.dispose();
  });
});
