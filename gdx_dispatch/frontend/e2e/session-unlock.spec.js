// GDXA-1187: the quick-PIN lock gate, walked in a real browser.
//
// The lock is set by the Android app (cold start, pause), which a desktop
// browser cannot be; this seeds the same localStorage state the app writes
// (PIN on, session locked) and walks what the person then sees: every page
// held on /unlock, a wrong PIN refused, the right PIN going on to the page.
// Run against a throwaway container (GDX_E2E_BYPASS=1, GDX_ENV=dev).
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

const CASES = [
  { who: 'technician', email: process.env.E2E_TECH_EMAIL || 'e2e_tech@gdx.internal', path: '/mobile' },
  { who: 'office', email: process.env.E2E_EMAIL || 'e2e_pw@gdx.internal', path: '/dashboard' },
];

const VIEWPORTS = [
  { name: 'desktop', viewport: { width: 1280, height: 800 } },
  { name: 'mobile', viewport: { width: 412, height: 915 } }, // Pixel 8 CSS size
];

for (const vp of VIEWPORTS) test.describe(vp.name, () => {
test.use({ viewport: vp.viewport });
for (const c of CASES) {
  for (const theme of ['light', 'dark']) {
    test(`${c.who} (${theme}): a locked session is held on /unlock until the PIN`, async ({ page, baseURL }) => {
      const api = await pwRequest.newContext({ baseURL });
      const r = await api.post('/auth/login', {
        headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
        data: { email: c.email, password: PASSWORD },
      });
      expect(r.ok()).toBeTruthy();
      const { access_token: token } = await r.json();
      await api.dispose();

      // Seed once per tab (a marker in sessionStorage), so the reload after
      // unlocking does not lock the session again.
      await page.addInitScript(async (a) => {
        if (window !== window.top || sessionStorage.getItem('e2e_seeded')) return;
        sessionStorage.setItem('e2e_seeded', '1');
        sessionStorage.setItem('gdx_access_token', a.token);
        sessionStorage.setItem('gdx_tenant_slug', a.tenant);
        localStorage.setItem('gdx_theme', a.theme);
        // nativeAuth.js hashes `gdx_pin_salt_<pin>` with SHA-256.
        const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('gdx_pin_salt_1234'));
        const hex = Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
        localStorage.setItem('gdx_mobile_quick_pin_hash', hex);
        localStorage.setItem('gdx_mobile_quick_pin_enabled', 'true');
        localStorage.setItem('gdx_mobile_session_locked', 'true');
      }, { token, tenant: TENANT, theme });

      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));

      await page.goto(c.path);
      await expect(page.locator('[data-test="mobile-pin-dialog"]')).toBeVisible({ timeout: 15000 });
      const held = new URL(page.url());
      expect(held.pathname).toBe('/unlock');
      expect(held.searchParams.get('redirect')).toBe(c.path);
      // No shell behind the gate: nothing of the app shows through.
      await expect(page.locator('.layout-sidebar, .layout-topbar')).toHaveCount(0);
      await page.screenshot({ path: `${SHOTS}/unlock-${c.who}-${theme}-${vp.name}.png` });

      // Another page is held too.
      await page.goto('/customers');
      await expect(page.locator('[data-test="mobile-pin-dialog"]')).toBeVisible({ timeout: 15000 });

      for (const d of '9999') await page.locator(`[data-test="key-${d}"]`).click();
      await expect(page.locator('[data-test="pin-error"]')).toBeVisible();
      await page.waitForTimeout(500);

      for (const d of '1234') await page.locator(`[data-test="key-${d}"]`).click();
      await expect(page.locator('[data-test="mobile-pin-dialog"]')).toHaveCount(0, { timeout: 15000 });
      expect(new URL(page.url()).pathname).not.toBe('/unlock');
      expect(await page.evaluate(() => localStorage.getItem('gdx_mobile_session_locked'))).toBeNull();
      expect(errors).toEqual([]);
    });
  }
}
});
