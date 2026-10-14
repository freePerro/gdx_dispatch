/**
 * A signed-out cold load draws no 401 (GDXA-1023).
 *
 * main.js mounts before the router's first navigation resolves. App.vue used
 * to mount the shell for that first tick, and the topbar bell poll and the
 * What's-new banner each fired an authenticated GET that 401'd before the guard
 * reached /login. The unit spec (src/__tests__/coldLoadSignedOut.spec.js)
 * proves it in jsdom and is the CI guard; this checks the real bundle, light
 * and dark, desktop and phone width, and runs by hand against a throwaway
 * container (ci.yml has no Playwright step). The signed-in half checks the
 * shell still arrives.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

// Reachable without a token.
const PUBLIC_API = new Set(['/api/settings/branding']);

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  phone: { width: 412, height: 915 },
};

function watchApi(page) {
  const seen = { unauthorized: [], privateCalls: [], calls: [] };
  page.on('response', (res) => {
    const { pathname } = new URL(res.url());
    if (!pathname.startsWith('/api/') && !pathname.startsWith('/auth/')) return;
    seen.calls.push(`${res.status()} ${pathname}`);
    if (res.status() === 401) seen.unauthorized.push(pathname);
    if (pathname.startsWith('/api/') && !PUBLIC_API.has(pathname)) seen.privateCalls.push(pathname);
  });
  return seen;
}

for (const [vpName, viewport] of Object.entries(VIEWPORTS)) {
  for (const theme of ['light', 'dark']) {
    for (const path of ['/dashboard', '/login', '/']) {
      test(`signed out ${path} (${vpName}, ${theme}) lands on /login with no 401`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.addInitScript((t) => {
          if (window !== window.top) return;
          localStorage.setItem('gdx_theme', t);
        }, theme);
        const seen = watchApi(page);
        await page.goto(path);
        await expect(page).toHaveURL(/\/login(\?|$)/);
        await expect(page.locator('input[type="password"]')).toBeVisible();
        await page.waitForLoadState('networkidle');
        expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
        expect(await page.locator('.app-topbar').count()).toBe(0);
        expect(seen.unauthorized, seen.calls.join('\n')).toEqual([]);
        expect(seen.privateCalls, seen.calls.join('\n')).toEqual([]);
        if (path === '/dashboard') {
          await page.screenshot({ path: `test-results/cold-load-signed-out-${vpName}-${theme}.png` });
        }
      });
    }
  }
}

test.describe('signed in', () => {
  let token;
  test.beforeAll(async ({ baseURL }) => {
    const api = await pwRequest.newContext({ baseURL });
    const r = await api.post('/auth/login', {
      headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(r.ok(), 'login should succeed').toBeTruthy();
    token = (await r.json()).access_token;
    await api.dispose();
  });

  for (const theme of ['light', 'dark']) {
    test(`cold load /dashboard still mounts the shell and polls the bell (${theme})`, async ({ page }) => {
      await page.setViewportSize(VIEWPORTS.desktop);
      await page.addInitScript((a) => {
        if (window !== window.top) return;
        sessionStorage.setItem('gdx_access_token', a.token);
        localStorage.setItem('gdx_theme', a.theme);
      }, { token, theme });
      const seen = watchApi(page);
      const bell = page.waitForResponse((res) => new URL(res.url()).pathname === '/api/notifications/count');
      await page.goto('/dashboard');
      await expect(page.locator('.app-topbar')).toBeVisible();
      expect((await bell).status()).toBe(200);
      await page.waitForLoadState('networkidle');
      expect(seen.unauthorized, seen.calls.join('\n')).toEqual([]);
      await page.screenshot({ path: `test-results/cold-load-signed-in-${theme}.png` });
    });
  }
});
