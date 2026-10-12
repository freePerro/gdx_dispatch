/**
 * GDXA-700 — the vacation allowance is set where an admin already is.
 *
 * Settings → Feature Settings → Time off and holidays carries the shop's
 * "Vacation hours per year"; Users → Edit Profile carries one person's
 * override. Each must save, survive a reload, and clear back to blank. The
 * values the run found are put back at the end. Screenshots go to
 * E2E_SHOT_DIR (default test-results/) in light and dark.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

async function login(baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token: token } = await r.json();
  const headers = { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT, 'content-type': 'application/json' };
  return { api, token, headers };
}

async function prime(page, token, theme) {
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: token, tid: TENANT, theme });
}

async function openTimeOff(page) {
  // The card is rendered before GET /api/settings lands; typing ahead of it
  // gets overwritten when the response is applied.
  const loaded = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/settings' && r.request().method() === 'GET');
  await page.goto('/settings');
  await loaded;
  await page.getByRole('tab', { name: 'Feature Settings' }).click();
  const card = page.locator('[data-testid="time-off-card"]');
  await expect(card).toBeVisible({ timeout: 15000 });
  return card;
}

async function openEdit(page, userId) {
  await page.goto('/users');
  await page.locator(`[data-testid="users-edit-${userId}"]`).click();
  const input = page.locator('[data-testid="users-edit-vacation-allowance"] input, input#edit-vacation-allowance').first();
  await expect(input).toBeVisible({ timeout: 15000 });
  return input;
}

// The field sits below the dialog's fold: scroll to it and shoot its row.
async function shotField(input, path) {
  await input.scrollIntoViewIfNeeded();
  await input.locator('xpath=ancestor::div[contains(@class,"form-field")][1]').screenshot({ path });
}

test('shop default and per-person override save, reload and clear', async ({ page, baseURL }) => {
  const { api, token, headers } = await login(baseURL);
  const before = await (await api.get('/api/settings', { headers })).json();
  const me = await (await api.get('/api/users/me', { headers })).json();
  try {
    await prime(page, token, 'light');

    // Shop default: type, save, reload, see it, then clear.
    let card = await openTimeOff(page);
    const shop = page.locator('input#time-off-vacation-allowance');
    await shop.fill('80.5');
    await shop.blur();
    await card.locator('[data-testid="time-off-save"]').click();
    await expect.poll(async () => (await (await api.get('/api/settings', { headers })).json()).vacation_allowance_hours).toBe(80.5);
    card = await openTimeOff(page);
    await expect(page.locator('input#time-off-vacation-allowance')).toHaveValue('80.5');
    // The input must not sit on top of its hint (it once overflowed a capped wrapper).
    const box = await page.locator('input#time-off-vacation-allowance').boundingBox();
    const hint = await page.locator('[data-testid="time-off-vacation-allowance-hint"]').boundingBox();
    const overlaps = box.x < hint.x + hint.width && hint.x < box.x + box.width
      && box.y < hint.y + hint.height && hint.y < box.y + box.height;
    expect(overlaps).toBe(false);
    await card.screenshot({ path: `${SHOTS}/vacation-settings-light.png` });

    // Per-person override: type, save, reopen, see it.
    let input = await openEdit(page, me.id);
    await input.fill('120');
    await input.blur();
    await page.locator('[data-testid="users-edit-submit"]').click();
    await expect.poll(async () => (await (await api.get(`/api/users/${me.id}`, { headers })).json()).vacation_allowance_hours).toBe(120);
    input = await openEdit(page, me.id);
    await expect(input).toHaveValue('120');
    await shotField(input, `${SHOTS}/vacation-user-light.png`);

    // Clearing the override hands the person back to the shop default.
    await input.fill('');
    await input.blur();
    await page.locator('[data-testid="users-edit-submit"]').click();
    await expect.poll(async () => (await (await api.get(`/api/users/${me.id}`, { headers })).json()).vacation_allowance_hours).toBe(null);

    // Dark mode, same two surfaces.
    const dark = await page.context().newPage();
    await prime(dark, token, 'dark');
    const darkCard = await openTimeOff(dark);
    expect(await dark.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
    await darkCard.screenshot({ path: `${SHOTS}/vacation-settings-dark.png` });
    await shotField(await openEdit(dark, me.id), `${SHOTS}/vacation-user-dark.png`);

    // Clearing the shop default saves null, not 0.
    card = await openTimeOff(page);
    await page.locator('input#time-off-vacation-allowance').fill('');
    await page.locator('input#time-off-vacation-allowance').blur();
    await card.locator('[data-testid="time-off-save"]').click();
    await expect.poll(async () => (await (await api.get('/api/settings', { headers })).json()).vacation_allowance_hours).toBe(null);
  } finally {
    await api.patch('/api/settings', { headers, data: { vacation_allowance_hours: before.vacation_allowance_hours ?? null } });
    await api.patch(`/api/users/${me.id}`, { headers, data: { vacation_allowance_hours: me.vacation_allowance_hours ?? null } });
    await api.dispose();
  }
});
