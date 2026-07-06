// Estimate detail must clearly show when an estimate has already been
// converted to a job: a "Converted to Job" tag, a banner with a working
// "View Job" link, and NO "Convert to Job" button (which would 409).
//
// Seeds a customer + estimate via API, accepts it (auto-converts to a job),
// then asserts on the rendered SPA. Credentials come from env
// (E2E_EMAIL/E2E_PASSWORD/E2E_TENANT_SLUG) so no secret is committed.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';

test('converted estimate shows indicator + job link, hides Convert button', async ({ page, baseURL }) => {
  test.skip(!EMAIL || !PASSWORD, 'E2E_EMAIL/E2E_PASSWORD not set');

  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: {
      'x-tenant-id': TENANT,
      'content-type': 'application/json',
      'x-e2e-test': 'true',
    },
  });

  const login = await api.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
  expect(login.ok(), `login: ${login.status()}`).toBeTruthy();
  const { access_token } = await login.json();
  const auth = { authorization: `Bearer ${access_token}` };

  const cust = await api.post('/api/customers', {
    headers: auth,
    data: { name: 'E2E Converted-Indicator Customer', phone: '5550009901' },
  });
  expect(cust.ok(), await cust.text()).toBeTruthy();
  const customerId = (await cust.json()).id;

  const est = await api.post('/api/estimates', {
    headers: auth,
    data: {
      customer_id: customerId,
      label: 'E2E converted-indicator estimate',
      line_items: [{ category: 'Doors', description: 'Test door', quantity: 1, unit_price: 500 }],
    },
  });
  expect(est.ok(), await est.text()).toBeTruthy();
  const estimateId = (await est.json()).id;

  // Accept → auto-converts to a job.
  const accept = await api.post(`/api/estimates/${estimateId}/accept`, { headers: auth });
  expect(accept.ok(), await accept.text()).toBeTruthy();
  const jobId = (await accept.json()).auto_converted_job_id;
  expect(jobId, 'estimate accept should auto-create a job').toBeTruthy();

  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });

  await page.goto(`/estimates/${estimateId}`);

  await expect(page.locator('[data-testid="estimate-converted-tag"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="estimate-converted-banner"]'))
    .toContainText('converted to a job');
  await expect(page.locator('[data-testid="estimate-convert-job"]')).toHaveCount(0);

  // The banner link navigates to the created job.
  await page.locator('[data-testid="estimate-view-job-link"]').click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${jobId}$`), { timeout: 15000 });

  await api.dispose();
});
