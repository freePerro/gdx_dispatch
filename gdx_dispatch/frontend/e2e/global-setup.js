// Runs once before all tests. Logs in, ensures the lab tenant has at least
// one of each entity type that param routes need, and writes both a
// storageState.json (Playwright auth) and a fixtures.json (param IDs).
import fs from 'node:fs';
import path from 'node:path';
import { request as pwRequest } from '@playwright/test';

const BASE = process.env.E2E_BASE_URL || 'http://localhost:8001';
const TENANT = process.env.E2E_TENANT_SLUG || 'lab-tenant';
const EMAIL = process.env.E2E_EMAIL || 'lab@example.com';
const PASSWORD = process.env.E2E_PASSWORD || 'changeme';
// Read-only mode: skip the POST-to-seed step. Required for any target that
// holds real data (prod, staging-with-business-data). Fixture IDs come
// from whatever the list endpoints already return; param routes with no
// existing data skip.
const READ_ONLY = process.env.E2E_READ_ONLY === '1';

const STORAGE_PATH = path.resolve('e2e/.state/storageState.json');
const FIXTURES_PATH = path.resolve('e2e/.state/fixtures.json');

async function login(ctx) {
  const res = await ctx.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT },
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!res.ok()) throw new Error(`login failed (${res.status()}): ${await res.text()}`);
  const { access_token } = await res.json();
  return access_token;
}

async function listFirstId(ctx, token, url) {
  const res = await ctx.get(url, {
    headers: { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT },
  });
  if (!res.ok()) return null;
  const json = await res.json();
  const items = Array.isArray(json) ? json : (json.items || json.results || []);
  return items.length ? (items[0].id || items[0].uuid || items[0].slug || null) : null;
}

async function ensure(ctx, token, listUrl, postUrl, body) {
  const existing = await listFirstId(ctx, token, listUrl);
  if (existing) return existing;
  if (READ_ONLY) {
    console.warn(`[seed] skipping POST ${postUrl} — E2E_READ_ONLY=1`);
    return null;
  }
  const res = await ctx.post(postUrl, {
    headers: {
      authorization: `Bearer ${token}`,
      'x-tenant-id': TENANT,
      'content-type': 'application/json',
    },
    data: body,
  });
  if (!res.ok()) {
    console.warn(`[seed] ${postUrl} → ${res.status()}: ${(await res.text()).slice(0, 200)}`);
    return null;
  }
  const j = await res.json();
  return j.id || j.uuid || null;
}

export default async function globalSetup() {
  fs.mkdirSync(path.dirname(STORAGE_PATH), { recursive: true });
  const ctx = await pwRequest.newContext({ baseURL: BASE });
  const token = await login(ctx);

  // Seed minimal data — idempotent, only POSTs when list is empty.
  const customerId = await ensure(
    ctx,
    token,
    '/api/customers',
    '/api/customers',
    { name: 'E2E Customer', email: 'e2e-customer@example.com', phone: '5550000001' },
  );

  const jobId = await ensure(
    ctx,
    token,
    '/api/jobs',
    '/api/jobs',
    {
      customer_id: customerId,
      title: 'E2E Smoke Job',
      description: 'Created by route-coverage harness',
      status: 'scheduled',
    },
  );

  const estimateId = await ensure(
    ctx,
    token,
    '/api/estimates',
    '/api/estimates',
    { customer_id: customerId, total: 100, status: 'draft' },
  );

  const invoiceId = await ensure(
    ctx,
    token,
    '/api/invoices',
    '/api/invoices',
    { customer_id: customerId, total: 100, status: 'draft' },
  );

  // Build a Playwright storageState that primes localStorage with the JWT
  // exactly the way the SPA's auth store expects it.
  const storageState = {
    cookies: [],
    origins: [
      {
        origin: BASE,
        localStorage: [
          { name: 'access_token', value: token },
          { name: 'tenant_id', value: TENANT },
        ],
      },
    ],
  };
  fs.writeFileSync(STORAGE_PATH, JSON.stringify(storageState, null, 2));

  const fixtures = {
    token,
    tenant: TENANT,
    ids: {
      customer: customerId,
      job: jobId,
      estimate: estimateId,
      invoice: invoiceId,
      gameSlug: 'audit-anomaly',
    },
    seededAt: new Date().toISOString(),
  };
  fs.writeFileSync(FIXTURES_PATH, JSON.stringify(fixtures, null, 2));

  console.log('[global-setup] auth + fixtures ready:', fixtures.ids);
  await ctx.dispose();
}
