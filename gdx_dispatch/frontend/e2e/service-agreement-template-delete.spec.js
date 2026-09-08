/**
 * Service Agreements → Templates: the trash button really deletes (#455, PR #674).
 *
 * The control has always existed and always sent
 * `DELETE /api/service-agreements/templates/{id}`, which nothing served — so
 * confirming the dialog produced a 405 and the row stayed. Only a browser
 * proves the whole chain, because `useDestructiveConfirm` auto-accepts under
 * vitest: the confirm dialog itself is never exercised there.
 *
 * Each test seeds its own template through the API, so the file is
 * re-runnable against the same database.
 */
import { expect, request as pwRequest, test } from '@playwright/test';

const ENV = process.env;
const TENANT = ENV.E2E_TENANT_SLUG;

async function apiContext(baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: ENV.E2E_EMAIL, password: ENV.E2E_PASSWORD },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token } = await r.json();
  return { api, token: access_token };
}

async function signInAndSeed(page, baseURL, name) {
  const { api, token } = await apiContext(baseURL);
  const created = await api.post('/api/service-agreements/templates', {
    headers: {
      'content-type': 'application/json',
      'x-tenant-id': TENANT,
      authorization: `Bearer ${token}`,
    },
    data: {
      name,
      description: 'Seeded by the walk',
      default_duration_months: 12,
      default_price: 299,
      services_included: ['Spring inspection', 'Lube service'],
    },
  });
  expect(created.status(), 'seed template').toBe(201);
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: token, tid: TENANT });
  await api.dispose();
}

/** Open the Templates tab and wait for the tab's own content, not for a row. */
async function gotoTemplates(page) {
  await page.goto('/service-agreements');
  const tab = page.getByRole('tab', { name: /templates/i }).first();
  await expect(tab).toBeVisible({ timeout: 20000 });
  await tab.click();
  await expect(page.getByRole('button', { name: /new template/i }).first())
    .toBeVisible({ timeout: 20000 });
}

function rowFor(page, name) {
  return page.locator('tr', { hasText: name }).first();
}

function trashIn(page, name) {
  return rowFor(page, name).locator('button').filter({ has: page.locator('.pi-trash') }).first();
}

test('deleting a template asks first, then really removes it', async ({ page, baseURL }) => {
  const name = `Walk Template ${Date.now()}`;
  await signInAndSeed(page, baseURL, name);
  await gotoTemplates(page);
  await expect(rowFor(page, name)).toBeVisible({ timeout: 20000 });

  const responses = [];
  page.on('response', (r) => {
    if (r.url().includes('/api/service-agreements/templates')) {
      responses.push(`${r.request().method()} ${r.status()}`);
    }
  });

  await trashIn(page, name).click();
  const dialog = page.locator('.p-confirmdialog');
  await expect(dialog).toBeVisible({ timeout: 10000 });
  await dialog.getByRole('button', { name: /^delete$/i }).click();

  // the row is gone...
  await expect(page.locator('tr', { hasText: name })).toHaveCount(0, { timeout: 15000 });
  // ...the DELETE was answered 204, not the 405 this issue is about...
  await expect.poll(() => responses.some((r) => r === 'DELETE 204')).toBe(true);
  expect(responses.some((r) => r.startsWith('DELETE 4')), 'no 4xx on the delete').toBe(false);

  // ...and it stays gone across a reload (a real write, not a UI splice).
  await page.reload();
  await gotoTemplates(page);
  await expect(page.locator('tr', { hasText: name })).toHaveCount(0);
});

test('cancelling the confirm leaves the template alone', async ({ page, baseURL }) => {
  const name = `Walk Keep ${Date.now()}`;
  await signInAndSeed(page, baseURL, name);
  await gotoTemplates(page);
  await expect(rowFor(page, name)).toBeVisible({ timeout: 20000 });

  await trashIn(page, name).click();
  const dialog = page.locator('.p-confirmdialog');
  await expect(dialog).toBeVisible({ timeout: 10000 });
  await dialog.getByRole('button', { name: /cancel/i }).click();
  await expect(dialog).toBeHidden();

  await page.reload();
  await gotoTemplates(page);
  await expect(rowFor(page, name)).toBeVisible({ timeout: 20000 });
});
