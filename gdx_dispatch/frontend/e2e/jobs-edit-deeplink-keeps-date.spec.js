// Jobs list ?edit=<id> deep link — saving must keep the job's date (GDXA-371).
//
// The list is fetched once (it asks for per_page=1000; the server returns at
// most 500) and ?edit= looks the job up there first. A job outside those rows
// fell back to GET /api/jobs/{id}, whose raw row has scheduled_at but not the
// list's scheduledDate, so the edit form
// opened dateless and Save PATCHed scheduled_at: null — clearing the date and
// the appointment with it. This walk forces the fallback by answering the
// list request with no rows, then saves untouched and re-reads the job.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

async function login(baseURL) {
  const bootstrap = await pwRequest.newContext({ baseURL });
  const r = await bootstrap.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await bootstrap.dispose();
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: { authorization: `Bearer ${access_token}`, 'x-e2e-test': 'true' },
  });
  return { api, access_token };
}

test('saving from an ?edit= deep link outside the list keeps the date', async ({ page, baseURL }) => {
  const { api, access_token } = await login(baseURL);
  const json = { 'content-type': 'application/json' };

  const custBody = await (await api.get('/api/customers?per_page=1', { headers: json })).json();
  const customers = Array.isArray(custBody) ? custBody : custBody.items || [];
  expect(customers.length).toBeGreaterThan(0);
  const techBody = await (await api.get('/api/technicians', { headers: json })).json();
  const techs = Array.isArray(techBody) ? techBody : techBody.items || techBody.data || [];

  const when = new Date(Date.now() + 3 * 86400000);
  when.setUTCHours(15, 30, 0, 0);
  const created = await api.post('/api/jobs', {
    headers: json,
    data: {
      title: `E2E deep-link date ${Date.now()}`,
      customer_id: customers[0].id,
      job_type: 'Service Call',
      scheduled_at: when.toISOString(),
      ...(techs.length ? { assigned_tech_ids: [String(techs[0].id)] } : {}),
    },
  });
  expect(created.ok()).toBeTruthy();
  const job = await created.json();
  const jobId = job.id || job.job?.id;
  const before = await (await api.get(`/api/jobs/${jobId}`, { headers: json })).json();
  expect(before.scheduled_at).toBeTruthy();

  // Answer the list request with no rows, so ?edit= must take the fallback.
  await page.route(
    (url) => url.pathname === '/api/jobs' && url.searchParams.has('per_page'),
    (route) => route.fulfill({ json: { items: [], total: 0, status_counts: {} } }),
  );
  const fallback = page.waitForResponse((r) => new URL(r.url()).pathname === `/api/jobs/${jobId}`);
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    if (a.tid) sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });
  await page.goto(`/jobs?edit=${jobId}`);
  await fallback;

  const dialog = page.getByTestId('jobs-form-dialog');
  await expect(dialog).toBeVisible({ timeout: 15000 });
  // The form opened holding the job's date, not an empty picker.
  await expect(dialog.getByTestId('job-scheduled-input').locator('input')).not.toHaveValue('');

  const patched = page.waitForRequest(
    (r) => r.method() === 'PATCH' && new URL(r.url()).pathname === `/api/jobs/${jobId}`,
  );
  await dialog.getByTestId('job-submit-btn').click();
  const body = (await patched).postDataJSON();
  expect(body.scheduled_at).toBe(new Date(before.scheduled_at).toISOString());
  await expect(dialog).toBeHidden({ timeout: 15000 });

  const after = await (await api.get(`/api/jobs/${jobId}`, { headers: json })).json();
  expect(new Date(after.scheduled_at).getTime()).toBe(new Date(before.scheduled_at).getTime());

  await api.dispose();
});
