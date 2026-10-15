// GDXA-1181: the Ledger page's trial-balance tab shows the receivables
// tie-out (open invoices vs the AR account), in light and dark, on a desk
// and at phone width. Read-only: it posts nothing.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

async function signIn(page, baseURL, theme) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token: token } = await r.json();
  await api.dispose();
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.token);
    sessionStorage.setItem('gdx_tenant_slug', a.tenant);
    localStorage.setItem('gdx_theme', a.theme);
  }, { token, tenant: TENANT, theme });
}

for (const theme of ['light', 'dark']) {
  for (const [label, viewport] of [['desktop', { width: 1366, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
    test(`receivables tie-out renders (${theme}, ${label})`, async ({ page, baseURL }) => {
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.setViewportSize(viewport);
      await signIn(page, baseURL, theme);
      await page.goto('/accounting-ledger');
      const section = page.locator('[data-testid="ar-control"]');
      await expect(section).toBeVisible({ timeout: 20000 });
      await expect(page.locator('[data-testid="ar-control-tag"]')).toHaveText(/^(Tied|NOT TIED: )/);
      await expect(page.locator('[data-testid="ar-control-totals"]')).toContainText('Open invoices');
      expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
      // The page itself must not scroll sideways; a wide table may scroll inside itself.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
      await section.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${SHOTS}/ar-control-${theme}-${label}.png`, fullPage: false });
      expect(errors).toEqual([]);
    });
  }
}
