// Visual pass for the schedule dialog + dispatch queue in BOTH themes.
// Not an assertion suite — it renders and screenshots so the change can be
// eyeballed for contrast/theming bugs (Doug runs the app in dark mode).
import { test, request as pwRequest, expect } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

test('screenshots: schedule dialog + dispatch queue, light and dark', async ({ page, baseURL }) => {
  const bootstrap = await pwRequest.newContext({ baseURL });
  const r = await bootstrap.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: {
      authorization: `Bearer ${access_token}`,
      'x-e2e-test': 'true',
    },
  });

  const custBody = await (await api.get('/api/customers?per_page=1')).json();
  const customers = Array.isArray(custBody) ? custBody : custBody.items || [];
  const job = await (await api.post('/api/jobs', {
    headers: { 'content-type': 'application/json' },
    data: { title: `Visual schedule ${Date.now()}`, customer_id: customers[0].id, job_type: 'Service Call' },
  })).json();

  await page.setViewportSize({ width: 1500, height: 1000 });

  // Surface anything the page failed to load — an empty board and a
  // "Something went wrong" toast look identical to a rendering bug otherwise.
  const failures = [];
  page.on('response', (res) => {
    if (res.status() >= 400) failures.push(`${res.status()} ${res.url().replace(baseURL, '')}`);
  });
  page.on('console', (m) => { if (m.type() === 'error') failures.push(`console: ${m.text().slice(0, 160)}`); });

  for (const theme of ['light', 'dark']) {
    await page.addInitScript((t) => { localStorage.setItem('gdx_theme', t); }, theme);
    await page.addInitScript((a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
    }, { t: access_token, tid: TENANT });

    await page.goto(`/jobs/${job.id}?schedule=1`);
    await expect(page.locator('[data-testid="job-schedule-dialog"]')).toBeVisible({ timeout: 20000 });
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    await page.screenshot({ path: `test-results/schedule-dialog-${theme}.png` });

    await page.goto('/dispatch');
    await expect(page.locator('[data-testid="unassigned-section"]')).toBeVisible({ timeout: 20000 });
    // The section renders empty on first paint; wait for the queue to actually
    // fill or the screenshot captures a pre-fetch board.
    await expect(page.locator(`[data-testid="unassigned-job-${job.id}"]`)).toBeVisible({ timeout: 20000 });
    await page.screenshot({ path: `test-results/dispatch-queue-${theme}.png` });
  }

  console.log('[visual] request/console failures:', failures.length ? failures : 'none');

  await api.dispose();
  await bootstrap.dispose();
});
