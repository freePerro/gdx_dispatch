// #56 — Estimate→Job conversion must copy line items onto the job.
//
// Runs against a live container (the API surface, end-to-end through real
// auth + DB). Creates a customer + estimate with two lines (one carrying a
// CHI-style line_metadata spec), accepts the estimate (which auto-creates the
// job), then asserts the job's parts-needed list mirrors the estimate lines —
// including sku/vendor pulled from line_metadata and the scalar spec carried
// into notes.
//
// Credentials come from env (E2E_EMAIL/E2E_PASSWORD/E2E_TENANT_SLUG) so no
// secret is committed. See run-e2e-56.sh for the wrapper that injects them.
import { test, expect, request as pwRequest } from '@playwright/test';

const BASE = process.env.E2E_BASE_URL || 'http://localhost:8001';
const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';

test('estimate→job conversion copies line items onto the job (#56)', async () => {
  test.skip(!EMAIL || !PASSWORD, 'E2E_EMAIL/E2E_PASSWORD not set');

  const ctx = await pwRequest.newContext({
    baseURL: BASE,
    extraHTTPHeaders: { 'x-tenant-id': TENANT, 'content-type': 'application/json' },
  });

  // Auth
  const login = await ctx.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
  expect(login.ok(), `login: ${login.status()}`).toBeTruthy();
  const { access_token } = await login.json();
  const auth = { authorization: `Bearer ${access_token}` };

  // Customer → estimate
  const cust = await ctx.post('/api/customers', {
    headers: auth,
    data: { name: 'E2E #56 Customer', email: 'e2e56@example.com', phone: '5550005600' },
  });
  expect(cust.ok(), await cust.text()).toBeTruthy();
  const customerId = (await cust.json()).id;

  const est = await ctx.post('/api/estimates', {
    headers: auth,
    data: { customer_id: customerId, label: 'E2E #56 Estimate', notes: 'line-copy check' },
  });
  expect(est.ok(), await est.text()).toBeTruthy();
  const estimateId = (await est.json()).id;

  // Two lines — one plain part, one with captured spec metadata.
  for (const line of [
    { description: 'Torsion Spring', quantity: 2, unit_price: 120.0, category: 'parts' },
    { description: 'CHI Door 16x7', quantity: 1, unit_price: 1850.0,
      line_metadata: { sku: 'CHI-2216', vendor: 'CHI', color: 'white' } },
  ]) {
    const r = await ctx.post(`/api/estimates/${estimateId}/lines`, { headers: auth, data: line });
    expect(r.ok(), await r.text()).toBeTruthy();
  }

  // Accept → auto-creates the job
  const accept = await ctx.post(`/api/estimates/${estimateId}/accept`, { headers: auth });
  expect(accept.ok(), await accept.text()).toBeTruthy();
  const jobId = (await accept.json()).auto_converted_job_id;
  expect(jobId, 'estimate accept should auto-create a job').toBeTruthy();

  // The job's parts-needed must mirror the estimate lines.
  const parts = await ctx.get(`/api/jobs/${jobId}/parts-needed`, { headers: auth });
  expect(parts.ok(), await parts.text()).toBeTruthy();
  const rows = await parts.json();
  const byName = Object.fromEntries(rows.map((p) => [p.part_name, p]));

  expect(Object.keys(byName).sort()).toEqual(['CHI Door 16x7', 'Torsion Spring']);
  expect(byName['Torsion Spring'].quantity).toBe(2);
  const door = byName['CHI Door 16x7'];
  expect(door.sku).toBe('CHI-2216');
  expect(door.supplier).toBe('CHI');
  expect(door.notes || '').toContain('color=white');

  await ctx.dispose();
});
