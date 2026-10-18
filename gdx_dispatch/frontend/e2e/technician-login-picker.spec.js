// GDXA-1583: a technician is created against a real login, picked in the
// New Technician dialog — never an email or a name slug. The server refuses a
// user_id with no user behind it.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

test('New Technician links the picked login and the server refuses a slug', async ({ page, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token } = await login.json();
  const hdr = { authorization: `Bearer ${access_token}`, 'x-tenant-id': TENANT, 'content-type': 'application/json' };

  // The slug the old form stored is refused, and nothing is written.
  const slug = await api.post('/api/technicians', { headers: hdr, data: { user_id: 'walk1583.pick.tech', name: 'Walk Slug' } });
  expect(slug.status()).toBe(400);

  const stamp = Date.now();
  const made = await api.post('/api/users', {
    headers: hdr,
    data: { name: `Pick Walk ${stamp}`, email: `pick.walk.${stamp}@example.com`, password: 'longenough1', role: 'technician' },
  });
  expect(made.ok()).toBeTruthy();
  const user = await made.json();

  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: access_token, tid: TENANT, theme: process.env.E2E_THEME || 'light' });
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto('/technicians');
  await page.locator('[data-testid="new-tech-btn"]').click();
  const save = page.locator('[data-testid="tech-save-btn"]');
  await expect(save).toBeDisabled();

  await page.locator('[data-testid="tech-user-select"]').click();
  await page.locator('.p-select-filter').fill(`pick.walk.${stamp}`);
  await page.getByRole('option', { name: new RegExp(`pick\\.walk\\.${stamp}`) }).click();
  await expect(page.locator('.p-dialog input[placeholder="Full name"]')).toHaveValue(`Pick Walk ${stamp}`);
  await expect(save).toBeEnabled();
  await page.screenshot({ path: `${SHOTS}/tech-picker-${process.env.E2E_THEME || 'light'}.png` });
  await save.click();
  await expect(page.locator('.p-dialog')).toBeHidden();

  const rows = await (await api.get('/api/technicians', { headers: hdr })).json();
  const row = rows.find((t) => t.name === `Pick Walk ${stamp}`);
  expect(row?.user_id).toBe(user.id);

  // Leave the local stack as found: soft-delete the row and the login.
  await api.delete(`/api/technicians/${row.id}`, { headers: hdr });
  await api.delete(`/api/users/${user.id}`, { headers: hdr });
  await api.dispose();
});
