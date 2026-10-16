// GDXA-1200 — the Settings invite dialog and UsersView offer Sales, and an
// invite with Sales goes through. Runs against a throwaway container; the
// invited user is soft-deleted at the end.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

for (const theme of ['light', 'dark']) {
  test(`role pickers offer Sales and a Sales invite succeeds (${theme})`, async ({ page, baseURL }) => {
    const api = await pwRequest.newContext({ baseURL });
    const login = await api.post('/auth/login', {
      headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(login.ok()).toBeTruthy();
    const { access_token: token } = await login.json();
    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    }, { t: token, tid: TENANT, theme });

    // Settings → Users tab → Invite User.
    await page.goto('/settings?tab=users');
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    await page.getByRole('button', { name: /invite user/i }).click();
    const dialog = page.locator('[data-testid="invite-user-dialog"]');
    await expect(dialog).toBeVisible({ timeout: 15000 });
    await dialog.locator('[data-testid="invite-role-dropdown"]').click();
    const options = page.locator('.p-select-overlay [role="option"]');
    await expect(options).toHaveText(['Admin', 'Dispatcher', 'Technician', 'Sales', 'Viewer']);
    await page.screenshot({ path: `${SHOTS}/settings-invite-${theme}.png` });
    await options.filter({ hasText: 'Sales' }).click();

    const email = `e2e.sales.${theme}.${Date.now()}@example.com`;
    await dialog.locator('[data-testid="invite-email-input"]').fill(email);
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/api/admin/users/invite')),
      dialog.locator('[data-testid="invite-submit-btn"]').click(),
    ]);
    expect(resp.status()).toBe(201);
    const body = await resp.json();
    expect(body.role).toBe('sales');
    // Either the copyable link (Redis up) or the warn toast (Redis down).
    await expect(page.locator('[data-testid="invite-link-dialog"], .p-toast-message').first()).toBeVisible();

    // /users: the new sales user is counted and its picker shows Sales.
    await page.goto('/users');
    // Wait for the new user's row: the stat cards render 0 until /api/users lands.
    const picker = page.locator(`[data-testid="users-role-select-${body.id}"]`);
    await expect(picker).toContainText('Sales', { timeout: 15000 });
    const stat = page.locator('[data-testid="users-stat-sales"] .stat-count');
    expect(Number(await stat.textContent())).toBeGreaterThanOrEqual(1);
    await picker.click();
    await expect(page.locator('.p-select-overlay [role="option"]')).toHaveText(
      ['Owner', 'Admin', 'Dispatcher', 'Technician', 'Sales', 'Viewer'],
    );
    await page.screenshot({ path: `${SHOTS}/users-${theme}.png` });
    await page.keyboard.press('Escape');

    // Clean up: soft-delete the invited user.
    const del = await api.delete(`/api/users/${body.id}`, {
      headers: { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    });
    expect(del.ok()).toBeTruthy();
    await api.dispose();
  });
}
