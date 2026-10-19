/**
 * GDXA-1827 — a hire date and the company's vacation year, set where an
 * admin already is.
 *
 * Users → Edit Profile carries "Hire date"; Settings → Feature Settings →
 * Time off and holidays carries "Vacation year starts" (calendar year or each
 * person's hire anniversary). Each must save, survive a reload, and the hire
 * date must clear back to blank. The values the run found are put back at the
 * end. Screenshots go to E2E_SHOT_DIR (default test-results/) in light and
 * dark, desktop and 390px.
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
  // The card renders before GET /api/settings lands; a choice made ahead of
  // it is overwritten when the response is applied.
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
  const input = page.locator('[data-testid="users-edit-hire-date"]');
  await expect(input).toBeVisible({ timeout: 15000 });
  return input;
}

async function shotField(locator, path) {
  await locator.scrollIntoViewIfNeeded();
  await locator.locator('xpath=ancestor::div[1]').screenshot({ path });
}

test('hire date and vacation year save, reload, and the hire date clears', async ({ page, baseURL }) => {
  const { api, token, headers } = await login(baseURL);
  const before = await (await api.get('/api/settings', { headers })).json();
  const me = await (await api.get('/api/users/me', { headers })).json();
  const policy = async () => (await (await api.get('/api/settings', { headers })).json()).vacation_year_policy;
  const hireDate = async () => (await (await api.get(`/api/users/${me.id}`, { headers })).json()).hire_date;
  try {
    await prime(page, token, 'light');

    // Company setting: starts on the calendar year, choose anniversary, save, reload.
    let card = await openTimeOff(page);
    const select = page.locator('[data-testid="time-off-vacation-year"]');
    await expect(select).toContainText('January 1 (calendar year)');
    await select.click();
    await page.getByRole('option', { name: "Each person's hire anniversary" }).click();
    await card.locator('[data-testid="time-off-save"]').click();
    await expect.poll(policy).toBe('hire_anniversary');
    card = await openTimeOff(page);
    await expect(page.locator('[data-testid="time-off-vacation-year"]')).toContainText("Each person's hire anniversary");
    await shotField(page.locator('[data-testid="time-off-vacation-year"]'), `${SHOTS}/vacation-year-light.png`);

    // Hire date: type, save, reopen, see it, then clear.
    let input = await openEdit(page, me.id);
    await input.fill('2019-04-02');
    await page.locator('[data-testid="users-edit-submit"]').click();
    await expect.poll(hireDate).toBe('2019-04-02');
    input = await openEdit(page, me.id);
    await expect(input).toHaveValue('2019-04-02');
    await shotField(input, `${SHOTS}/hire-date-light.png`);
    await input.fill('');
    await page.locator('[data-testid="users-edit-submit"]').click();
    await expect.poll(hireDate).toBe(null);

    // Dark mode, desktop and phone width, both surfaces.
    for (const [label, viewport] of [['desktop', { width: 1280, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
      const dark = await page.context().newPage();
      await dark.setViewportSize(viewport);
      await prime(dark, token, 'dark');
      await openTimeOff(dark);
      expect(await dark.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
      const sel = dark.locator('[data-testid="time-off-vacation-year"]');
      await expect(sel).toBeVisible();
      for (const tid of ['time-off-vacation-year', 'time-off-vacation-year-hint']) {
        const b = await dark.locator(`[data-testid="${tid}"]`).boundingBox();
        expect(b.x + b.width, `${tid} right edge at ${label}`).toBeLessThanOrEqual(viewport.width);
      }
      await shotField(sel, `${SHOTS}/vacation-year-dark-${label}.png`);
      const hire = await openEdit(dark, me.id);
      // On a phone the field must fit inside the dialog, not run off its edge.
      const box = await hire.boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
      await shotField(hire, `${SHOTS}/hire-date-dark-${label}.png`);
      await dark.close();
    }
  } finally {
    await api.patch('/api/settings', { headers, data: { vacation_year_policy: before.vacation_year_policy || 'calendar_year' } });
    await api.patch(`/api/users/${me.id}`, { headers, data: { hire_date: me.hire_date ?? null } });
    await api.dispose();
  }
});
