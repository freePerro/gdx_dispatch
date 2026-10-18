// A deploy window answers every API call with a gateway 5xx. Each failing
// call and poll used to stack its own "Something went wrong" toast; the
// outage now shows one per window (GDXA-1490). Light and dark, desktop and a
// phone-sized viewport.
import { test, expect } from './_fixtures.js';

const ROUTES = ['/jobs', '/customers', '/invoices', '/estimates', '/'];

for (const theme of ['light', 'dark']) {
  for (const [label, viewport] of [['desktop', { width: 1366, height: 900 }], ['mobile', { width: 412, height: 915 }]]) {
    test(`a 5xx outage shows one toast — ${theme}, ${label}`, async ({ page }, testInfo) => {
      await page.setViewportSize(viewport);
      await page.addInitScript((t) => {
        if (window !== window.top) return;
        localStorage.setItem('gdx_theme', t);
      }, theme);
      await page.goto('/');
      await page.waitForFunction(() => document.querySelector('#app')?.__vue_app__);
      await page.waitForLoadState('networkidle');
      expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);

      let served = 0;
      await page.route('**/api/**', async (route) => {
        if (route.request().url().includes('/api/feedback/client-error')) return route.fulfill({ status: 204 });
        served += 1;
        return route.fulfill({ status: 502, contentType: 'text/html', body: '<html>502 Bad Gateway</html>' });
      });

      // Client-side navigation: each view fires its own calls, as polls would.
      for (const path of ROUTES) {
        await page.evaluate((p) => document.querySelector('#app').__vue_app__.config.globalProperties.$router.push(p), path);
        await page.waitForTimeout(300);
      }
      await page.waitForTimeout(500);

      const toasts = page.locator('.p-toast-message', { hasText: 'Something went wrong' });
      expect(served).toBeGreaterThan(1);
      await expect(toasts).toHaveCount(1);
      await page.screenshot({ path: testInfo.outputPath(`outage-toast-${theme}-${label}.png`) });
      testInfo.annotations.push({ type: 'served-502s', description: String(served) });
    });
  }
}
