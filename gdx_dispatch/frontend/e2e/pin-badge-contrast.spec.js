// GDXA-1754: the sidebar quick-pin unread badge (.pin-badge) fills with
// --interactive-primary. White text on dark #63b3ff was 2.2:1; it now reads
// --text-on-interactive-primary. Measure the rendered colours in both themes.
// The unread count is stubbed so the badge renders without seeding mail.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

function luminance(rgb) {
  const [r, g, b] = rgb.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

for (const theme of ['dark', 'light']) {
  test(`pin badge text meets AA contrast in ${theme} mode`, async ({ page, baseURL }, testInfo) => {
    const api = await pwRequest.newContext({ baseURL });
    const r = await api.post('/auth/login', {
      headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(r.ok()).toBeTruthy();
    const { access_token } = await r.json();
    await api.dispose();

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.route('**/api/outlook/messages/unread-count', (route) =>
      route.fulfill({ json: { count: 7 } }));
    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    }, { t: access_token, tid: TENANT, theme });

    await page.goto('/');
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    const badge = page.locator('[data-testid="email-unread-badge"]');
    await expect(badge).toBeVisible({ timeout: 20000 });
    await expect(badge).toHaveText('7');

    const { color, background } = await badge.evaluate((el) => {
      const s = getComputedStyle(el);
      return { color: s.color, background: s.backgroundColor };
    });
    const ratio = contrast(color, background);
    testInfo.annotations.push({ type: theme, description: `${color} on ${background} = ${ratio.toFixed(2)}:1` });
    console.log(`${theme}: ${color} on ${background} = ${ratio.toFixed(2)}:1`);
    expect(ratio).toBeGreaterThanOrEqual(4.5);

    await page.locator('.pinned-item', { has: badge }).screenshot({
      path: testInfo.outputPath(`pin-badge-${theme}.png`),
    });
  });
}
