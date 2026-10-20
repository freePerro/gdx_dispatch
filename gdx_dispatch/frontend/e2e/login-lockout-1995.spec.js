/**
 * GDXA-1995 — ten wrong passwords lock an account for 15 minutes, and the
 * office can see and lift the lock on the Users page.
 *
 * Creates its own technician, locks it through /auth/login, checks the 11th
 * attempt (right password) is refused, then on /users: the Locked badge, its
 * "Too many failed sign-ins" reason, and Unlock clearing it; the right
 * password then signs in. The technician is deleted at the end. Screenshots
 * go to E2E_SHOT_DIR (default test-results/) in light and dark.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';
const LOGIN_HEADERS = { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' };

async function prime(page, token, theme) {
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: token, tid: TENANT, theme });
}

test('a login lockout shows on Users and Unlock lifts it', async ({ browser, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const admin = await api.post('/auth/login', { headers: LOGIN_HEADERS, data: { email: EMAIL, password: PASSWORD } });
  expect(admin.ok()).toBeTruthy();
  const { access_token: token } = await admin.json();
  const headers = { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT, 'content-type': 'application/json' };

  const techEmail = `lockout-${Date.now()}@example.com`;
  const techPw = 'Right-password-1995';
  const created = await api.post('/api/users', {
    headers, data: { name: 'Lockout Tech', email: techEmail, password: techPw, role: 'technician' },
  });
  expect(created.status(), await created.text()).toBe(201);
  const techId = (await created.json()).id;
  const signIn = (password) => api.post('/auth/login', { headers: LOGIN_HEADERS, data: { email: techEmail, password } });

  try {
    for (let i = 1; i <= 10; i += 1) {
      expect((await signIn(`wrong-${i}`)).status()).toBe(401);
    }
    const eleventh = await signIn(techPw);
    expect(eleventh.status()).toBe(401);
    expect((await eleventh.json()).detail).toBe('Invalid credentials');

    for (const theme of ['light', 'dark']) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await prime(page, token, theme);
      await page.goto('/users');
      const badge = page.locator(`[data-testid="users-locked-badge-${techId}"]`);
      await expect(badge).toBeVisible({ timeout: 15000 });
      await expect(page.locator(`[data-testid="users-unlock-${techId}"]`)).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
      await badge.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${SHOTS}/lockout-1995-row-${theme}.png` });

      await badge.click();
      const reason = page.locator('[data-testid="users-lockout-info-reason"]');
      await expect(reason).toContainText('Too many failed sign-ins');
      await page.screenshot({ path: `${SHOTS}/lockout-1995-reason-${theme}.png` });
      await page.locator('button').filter({ hasText: 'Close' }).click();
      await expect(reason).toBeHidden();

      if (theme === 'dark') {
        await page.locator(`[data-testid="users-unlock-${techId}"]`).click();
        await page.locator('[data-testid="users-unlock-submit"]').click();
        await expect(badge).toBeHidden({ timeout: 15000 });
        await page.reload();
        await expect(page.locator(`[data-testid="users-edit-${techId}"]`)).toBeVisible({ timeout: 15000 });
        await expect(badge).toBeHidden();
      }
      await ctx.close();
    }

    expect((await signIn(techPw)).status()).toBe(200);
  } finally {
    await api.delete(`/api/users/${techId}`, { headers });
    await api.dispose();
  }
});
