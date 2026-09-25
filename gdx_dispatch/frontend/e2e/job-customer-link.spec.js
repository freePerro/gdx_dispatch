/**
 * GDXA-16 — the office job page's customer name routes to the customer record.
 *
 * Unit tests prove the markup; only a browser proves the anchor is reachable,
 * navigates, and is legible in BOTH themes. The defect was a plain `<p>`: the
 * office could see who the job was for and had no way to open them.
 *
 * Run against a throwaway container serving the working tree:
 *   E2E_BASE_URL=http://localhost:8017 \
 *   E2E_EMAIL=e2e_pw@gdx.internal E2E_PASSWORD=$(cat ../../../scratch_e2e/pw.txt) \
 *   node_modules/.bin/playwright test e2e/job-customer-link.spec.js --retries=0
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const EMAIL = process.env.E2E_EMAIL || 'e2e_pw@gdx.internal';
const PASSWORD = process.env.E2E_PASSWORD;

// One login for the whole file: the auth rate limiter 429s a third login in
// quick succession, and the bypass header only works on a server started with
// GDX_E2E_BYPASS=1 — which a container cloned from the dev app's env is not.
let session = null;

async function signIn(page, baseURL) {
  if (!session) {
    const api = await pwRequest.newContext({ baseURL });
    const res = await api.post('/auth/login', {
      headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(res.ok(), `login failed: ${res.status()}`).toBeTruthy();
    const { access_token } = await res.json();
    const jobs = await api.get('/api/jobs?limit=50', {
      headers: { authorization: `Bearer ${access_token}` },
    });
    expect(jobs.ok()).toBeTruthy();
    const body = await jobs.json();
    const items = Array.isArray(body) ? body : body.items || [];
    const job = items.find((j) => j.customer_id);
    expect(job, 'no job with a customer in this tenant').toBeTruthy();
    await api.dispose();
    session = { token: access_token, job };
  }
  await page.addInitScript((t) => {
    sessionStorage.setItem('gdx_access_token', t);
  }, session.token);
  return session.job;
}

test('the customer name is a link to the customer record, and it navigates', async ({ page, baseURL }) => {
  const job = await signIn(page, baseURL);
  await page.goto(`/jobs/${job.id}`);

  const link = page.locator('[data-testid="job-detail-customer-link"]');
  await expect(link).toBeVisible({ timeout: 20_000 });
  await expect(link).toHaveAttribute('href', `/customers/${job.customer_id}`);
  expect((await link.innerText()).trim().length).toBeGreaterThan(0);

  await link.click();
  await expect(page).toHaveURL(new RegExp(`/customers/${job.customer_id}$`));
  // Landed on the real record, not a blank shell — the name is rendered there.
  await expect(page.locator('h1, h2, [data-testid="customer-name"]').first()).toBeVisible({
    timeout: 20_000,
  });
});

test('the link is legible in dark mode as well as light', async ({ page, baseURL }) => {
  const job = await signIn(page, baseURL);

  for (const mode of ['light', 'dark']) {
    await page.addInitScript((m) => localStorage.setItem('gdx_theme', m), mode);
    await page.goto(`/jobs/${job.id}`);
    await expect(page.locator('html')).toHaveAttribute('data-theme', mode);

    const link = page.locator('[data-testid="job-detail-customer-link"]');
    await expect(link).toBeVisible({ timeout: 20_000 });
    // Hardcoded light-mode colors are the common theming bug: assert the
    // anchor's own color differs from the surface it sits on in BOTH modes.
    const { color, bg } = await link.evaluate((el) => {
      const walk = (n) => {
        for (let p = n; p; p = p.parentElement) {
          const c = getComputedStyle(p).backgroundColor;
          if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c;
        }
        return 'rgb(255, 255, 255)';
      };
      return { color: getComputedStyle(el).color, bg: walk(el) };
    });
    expect(color, `${mode}: link colour equals its background`).not.toBe(bg);
  }
});

test('a job with no customer renders plain text, not a dead anchor', async ({ page, baseURL }) => {
  const job = await signIn(page, baseURL);
  // No lead-shaped job exists in this tenant, so the unassigned branch is
  // driven by intercepting the job read rather than by writing a row.
  await page.route(`**/api/jobs/${job.id}`, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({
      response: res,
      json: { ...body, customer_id: null, customer_name: null },
    });
  });

  await page.goto(`/jobs/${job.id}`);
  const name = page.locator('[data-testid="job-detail-customer-name"]');
  await expect(name).toBeVisible({ timeout: 20_000 });
  await expect(name).toHaveText('Unassigned');
  await expect(page.locator('[data-testid="job-detail-customer-link"]')).toHaveCount(0);
});
