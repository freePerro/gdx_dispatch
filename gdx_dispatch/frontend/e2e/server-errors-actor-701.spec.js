/**
 * Server Errors names people (#701).
 *
 * The login dict is {user_id, tenant_id, role} — no email, no name. The sink
 * copied an `email` claim nobody carries, so the page's User column was blank
 * for every error; resolving one recorded "system" as the resolver. The sink
 * now keeps ids and the page reads the person from the users row.
 *
 * Needs a server_errors row for the signed-in admin with no stored email —
 * the walk seeds one and passes its path in E2E_WALK_ERROR_PATH.
 */
import { expect, request as pwRequest, test } from '@playwright/test';

const ENV = process.env;
const TENANT = ENV.E2E_TENANT_SLUG;
const ERROR_PATH = ENV.E2E_WALK_ERROR_PATH;

test('the User column and the resolver name the person, not "system"', async ({ page, baseURL }) => {
  test.skip(!ERROR_PATH, 'set E2E_WALK_ERROR_PATH to a seeded server_errors row for the signed-in admin');
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: ENV.E2E_EMAIL, password: ENV.E2E_PASSWORD },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token: token } = await r.json();
  await page.addInitScript((t) => sessionStorage.setItem('gdx_access_token', t), token);

  await page.goto('/server-errors');
  await page.getByTestId('server-errors-path-filter').fill(ERROR_PATH);
  await page.getByTestId('server-errors-path-filter').press('Enter');
  const row = page.getByTestId('server-errors-table').locator('tbody tr', { hasText: ERROR_PATH });
  await expect(row).toBeVisible({ timeout: 15000 });
  // The sink stored no email; the page fills it from the users row.
  await expect(row).toContainText(ENV.E2E_EMAIL);

  await row.click();
  const dialog = page.getByTestId('server-errors-dialog');
  await expect(dialog).toBeVisible();
  await page.getByTestId('server-errors-resolve').click();
  await expect(page.getByTestId('server-errors-resolve')).toHaveCount(0, { timeout: 15000 });

  // Reopen from the Resolved tab: the dialog reads "Resolved by <person>".
  await page.getByTestId('server-errors-status-tabs').getByRole('button', { name: 'Resolved' }).click();
  const resolvedRow = page.getByTestId('server-errors-table').locator('tbody tr', { hasText: ERROR_PATH });
  await resolvedRow.click();
  const note = page.locator('.resolved-note');
  await expect(note).toBeVisible({ timeout: 15000 });
  await expect(note).not.toContainText('Resolved by system');
  await page.screenshot({ path: 'test-results/server-errors-701-light.png' });
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.reload();
  await expect(page.getByTestId('server-errors-table')).toBeVisible({ timeout: 15000 });
  await page.screenshot({ path: 'test-results/server-errors-701-dark.png' });
  await api.dispose();
});
