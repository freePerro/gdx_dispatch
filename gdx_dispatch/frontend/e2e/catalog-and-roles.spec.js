// API-context verification against a live container for issues #45, #49, #50,
// #55, #57. Exercises the real HTTP surface (auth + routers + DB) end-to-end.
//
// Credentials come from env (E2E_EMAIL/E2E_PASSWORD/E2E_TENANT_SLUG). See the
// throwaway-container harness in memory (e2e-playwright-on-throwaway-container).
import { test, expect, request as pwRequest } from '@playwright/test';

const BASE = process.env.E2E_BASE_URL || 'http://localhost:8001';
const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';

let ctx, auth;

test.beforeAll(async () => {
  test.skip(!EMAIL || !PASSWORD, 'E2E creds not set');
  ctx = await pwRequest.newContext({
    baseURL: BASE,
    extraHTTPHeaders: { 'x-tenant-id': TENANT, 'content-type': 'application/json' },
  });
  const login = await ctx.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
  expect(login.ok(), `login ${login.status()}`).toBeTruthy();
  auth = { authorization: `Bearer ${(await login.json()).access_token}` };
});

test.afterAll(async () => { if (ctx) await ctx.dispose(); });

async function newCatalog(name, extra = {}) {
  const r = await ctx.post('/api/catalogs', {
    headers: auth, data: { name, source_system: 'manual', product_class: 'parts', ...extra },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  return (await r.json());
}

test('#55 vendor field round-trips through the API', async () => {
  const cat = await newCatalog(`E2E Vendor ${Date.now()}`);
  const r = await ctx.post(`/api/catalogs/${cat.id}/items`, {
    headers: auth, data: { name: 'Spring', cost: 10, price: 20, vendor: 'Midwest Wholesale' },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  expect((await r.json()).vendor).toBe('Midwest Wholesale');
});

test('#50 catalog active toggle filters the picker', async () => {
  const cat = await newCatalog(`E2E Active ${Date.now()}`);
  await ctx.post(`/api/catalogs/${cat.id}/items`, {
    headers: auth, data: { name: 'PickerItem', cost: 5, price: 9 },
  });
  // New catalog defaults active.
  expect((await (await ctx.get(`/api/catalogs/${cat.id}`, { headers: auth })).json()).active).toBe(true);
  // Deactivate → items leave all-items picker.
  const patch = await ctx.patch(`/api/catalogs/${cat.id}`, { headers: auth, data: { active: false } });
  expect(patch.ok(), await patch.text()).toBeTruthy();
  const all = await (await ctx.get('/api/catalogs/all-items', { headers: auth })).json();
  expect(all.items.some((i) => i.catalog_id === cat.id)).toBe(false);
});

test('#49 delete catalog soft-deletes it', async () => {
  const cat = await newCatalog(`E2E Delete ${Date.now()}`);
  const del = await ctx.delete(`/api/catalogs/${cat.id}`, { headers: auth });
  expect(del.ok(), await del.text()).toBeTruthy();
  const get = await ctx.get(`/api/catalogs/${cat.id}`, { headers: auth });
  expect(get.status()).toBe(404);
});

test('#57 QB catalog sync is gated (409 when off)', async () => {
  const cat = await newCatalog(`E2E QBGate ${Date.now()}`);
  // Default off → pull is refused.
  await ctx.patch('/api/settings', {
    headers: auth, data: { integrations: { quickbooks_catalog_sync: false } },
  });
  const off = await ctx.post(`/api/catalogs/${cat.id}/sync/qb/pull`, {
    headers: auth, data: { items: [{ sku: 'X', name: 'X', cost: 1 }] },
  });
  expect(off.status()).toBe(409);
  // Enable → pull works.
  await ctx.patch('/api/settings', {
    headers: auth, data: { integrations: { quickbooks_catalog_sync: true } },
  });
  const on = await ctx.post(`/api/catalogs/${cat.id}/sync/qb/pull`, {
    headers: auth, data: { items: [{ sku: 'Y', name: 'Widget', cost: 10 }] },
  });
  expect(on.ok(), await on.text()).toBeTruthy();
  expect((await on.json()).created).toBe(1);
  // Restore default-off so we don't leave the tenant syncing.
  await ctx.patch('/api/settings', {
    headers: auth, data: { integrations: { quickbooks_catalog_sync: false } },
  });
});

test('#45 user-create normalizes role to canonical (long) form', async () => {
  const email = `e2e-role-${Date.now()}@example.com`;
  const r = await ctx.post('/api/users', {
    headers: auth, data: { email, name: 'Role Test', password: 'CorrectHorse9!', role: 'tech' },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  // Stored form is the canonical long 'technician', not the short 'tech' input.
  expect((await r.json()).role).toBe('technician');
});
